"""Who decides whether metrics are recorded and exported: the env-var pins.

Split out of :mod:`kiro_crew.metrics.provider`, which re-exports every name here
and stays the import path callers (and test patches) use. ``KIROCREW_TELEMETRY``
and ``DO_NOT_TRACK`` pin local recording; ``KIROCREW_TELEMETRY_DISABLED`` holds
the default off until ``telemetry.enabled`` is written; :func:`outbound_consented`
decides whether a recording host may also export.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Optional

from kiro_crew import beacon

if TYPE_CHECKING:
    from kiro_crew.config.loader import KiroCrewConfig

# The provider's logger, so moving these out changed no log record's origin.
logger = logging.getLogger("kiro_crew.metrics.provider")


# Env-var opt-in. ``KIROCREW_TELEMETRY`` lets a host turn
# LOCAL metrics on (or force them off) without editing ~/.kiro/crew/config.json —
# handy for CI, containers, and one-off debugging. Truthy => enable, falsy =>
# disable, unset/blank => defer to the ``telemetry.enabled`` config flag (itself
# default True). This gates LOCAL collection ONLY: external OTLP egress needs the
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
    if _heartbeat_env_opt_out_holds_recording_off():
        return False
    return None


def _heartbeat_env_opt_out_holds_recording_off() -> bool:
    """``KIROCREW_TELEMETRY_DISABLED`` also keeps the DEFAULT recording off.

    That variable was the env-var opt-out the dashboard showed before recording
    was on by default, so a host that set it has already said no to telemetry.
    It holds recording off only while nobody wrote ``telemetry.enabled``: a
    written choice (Settings, ``kirocrew telemetry enable``) still decides. The
    files are read only when the variable is set, so other hosts pay nothing.
    """
    if not beacon._env_truthy(beacon.DISABLE_ENV):
        return False
    try:
        from kiro_crew.config.loader import _ABSENT_AGENT_KEY, _stored_agent_value

        return _stored_agent_value("enabled", section="telemetry") is _ABSENT_AGENT_KEY
    except Exception:
        # Unreadable: fail toward off, which is what the variable asked for.
        return True


def env_pin_is_soft() -> bool:
    """Whether :func:`env_pin` comes only from ``KIROCREW_TELEMETRY_DISABLED``.

    That pin holds the DEFAULT off and gives way to a written
    ``telemetry.enabled``, so a control over that key still works: Settings and
    ``kirocrew telemetry status`` must not report it as an env var that decides
    on its own. ``DO_NOT_TRACK`` and ``KIROCREW_TELEMETRY`` are hard pins.
    """
    if beacon.do_not_track():
        return False
    raw = os.environ.get(TELEMETRY_ENV_VAR, "").strip().lower()
    if raw in _ENV_TRUTHY or raw in _ENV_FALSY:
        return False
    return _heartbeat_env_opt_out_holds_recording_off()


def pin_env_var() -> str:
    """Name the env var behind :func:`env_pin`, for a UI note that says which to unset."""
    if beacon.do_not_track():
        return beacon.DO_NOT_TRACK_ENV
    raw = os.environ.get(TELEMETRY_ENV_VAR, "").strip().lower()
    if (
        raw not in _ENV_TRUTHY
        and raw not in _ENV_FALSY
        and _heartbeat_env_opt_out_holds_recording_off()
    ):
        return beacon.DISABLE_ENV
    return TELEMETRY_ENV_VAR


def outbound_consented(cfg: KiroCrewConfig) -> bool:
    """Whether metrics may leave this machine at all, given that recording is on.

    Recording is local. Sending it anywhere needs an explicit opt-in to recording
    that the user made themselves: ``KIROCREW_TELEMETRY`` truthy, or
    ``telemetry.enabled: true`` written in a config file. A recording default
    alone never permits export, so a host with an export destination configured
    cannot start exporting just because the default changed.

    The Privacy chapter's acknowledgement (``dashboard.privacy_ack_version``) is
    deliberately NOT enough: today's notice text says recorded data stays on this
    machine, so agreeing to it is not agreeing to export. A notice that names the
    export destination would have to bump the version before it could count.

    Fails closed: anything unreadable answers False.
    """
    try:
        if env_pin() is True:
            return True
        if not cfg.telemetry.enabled:
            return False
        from kiro_crew.config.fields import field_default
        from kiro_crew.config.loader import TelemetryConfig as _TelemetryConfig
        from kiro_crew.config.loader import _stored_agent_value

        # A true value that is not the field's default can only be one somebody
        # chose; past that, ask the files whether ``true`` was written.
        if field_default(_TelemetryConfig, "enabled") is not True:
            return True
        return _stored_agent_value("enabled", section="telemetry") is True
    except Exception:
        logger.debug("outbound consent unresolvable; export disabled", exc_info=True)
        return False
