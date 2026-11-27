"""The bound on an app's ``setup.onEnableTimeout`` / ``onDisableTimeout`` scripts.

``manifest_sections.SetupConfig`` parses the value (``lifecycle_timeout_error``); the
enable route and the disable teardown apply the bound through
:func:`lifecycle_timeout_seconds`. Nothing imports this module when a manifest loads:
the ``manifest`` facade serves these names on first access, so a stdio MCP server's
listing never loads it.
"""

from __future__ import annotations

import logging
from typing import Any

from kiro_crew.apps.manifest_sections import (
    LIFECYCLE_SCRIPT_TIMEOUT_DEFAULT,
    LIFECYCLE_SCRIPT_TIMEOUT_MAX,
    lifecycle_timeout_error,
)


def clamp_lifecycle_timeout(value: Any) -> tuple[int, str]:
    """The seconds a lifecycle script gets for a manifest ``value``, and why it moved.

    The reason is ``""`` when ``value`` is used as written (``60``, ``"60"``,
    ``60.0``). Otherwise the seconds are clamped and the reason says how:

    - ``int()`` refuses it (see :func:`lifecycle_timeout_error`): the default.
    - a bool: the default, since ``int(True)`` is a 1-second timeout nobody meant.
    - below 1 after ``int()``: the default, since a zero or negative timeout ends the
      script before it starts.
    - above ``LIFECYCLE_SCRIPT_TIMEOUT_MAX``: the cap.
    - a fractional float: truncated, as ``int()`` always did, then the rules above.
    """
    if lifecycle_timeout_error("", value):
        return LIFECYCLE_SCRIPT_TIMEOUT_DEFAULT, "not a number"
    if isinstance(value, bool):
        return LIFECYCLE_SCRIPT_TIMEOUT_DEFAULT, "a bool is not a duration"
    seconds = int(value)
    fraction = isinstance(value, float) and value != seconds
    if seconds < 1:
        return LIFECYCLE_SCRIPT_TIMEOUT_DEFAULT, "below 1 second"
    if seconds > LIFECYCLE_SCRIPT_TIMEOUT_MAX:
        return LIFECYCLE_SCRIPT_TIMEOUT_MAX, f"above the {LIFECYCLE_SCRIPT_TIMEOUT_MAX}s cap"
    return seconds, ("fraction dropped" if fraction else "")


def lifecycle_timeout_seconds(value: Any, *, app_name: str = "", field_name: str = "") -> int:
    """:func:`clamp_lifecycle_timeout` where a script is about to run, logging a clamp.

    This is the only place the bound is applied. ``SetupConfig`` keeps the value as
    ``int()`` read it, because ``setup`` is part of ``AppManifest.signing_payload``:
    clamping it there would change the signed bytes of a manifest published with,
    say, ``600``, and its signature would stop verifying. The enable route and the
    disable teardown call this once per script run, so the warning is not repeated
    by listings. ``None`` (the key is absent) is the default with no warning.
    """
    if value is None:
        return LIFECYCLE_SCRIPT_TIMEOUT_DEFAULT
    seconds, reason = clamp_lifecycle_timeout(value)
    if reason:
        # The facade binds one ``logger`` (``manifest_sections``'), so none is defined here.
        logging.getLogger(__name__).warning(
            "app %r setup.%s=%r: %s, running the script with a %ss timeout",
            app_name,
            field_name,
            value,
            reason,
            seconds,
        )
    return seconds
