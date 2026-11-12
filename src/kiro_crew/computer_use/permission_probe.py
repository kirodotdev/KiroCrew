"""The Settings panel's permission probe: a separate process, run as ``python -m``.

    python -m kiro_crew.computer_use.permission_probe

Prints the driver's ADVISORY permission block as one JSON object on stdout
(``accessibility``, ``screen_recording``, ``responsible_hint``) and exits 0. The
dashboard's ``GET /api/computer-use/config`` spawns it with the gateway's own
interpreter and parses the line.

It runs out of process because the native probe is ctypes: a missing
``argtypes`` is a SIGSEGV rather than an exception, and inside the gateway that
would take every chat session, the cron scheduler and the Slack socket down with
it. Here the worst case is a permission row that reads "Could not check".

It is deliberately NOT ``kirocrew computer doctor --json``. That verb starts the
whole CLI (about 1,700 modules) and, with the feature on, projects the chat
backend's agent spec, which costs seconds: 3.5s warm and over 11s on a cold file
cache, measured on macOS. The gateway allows the probe 5s, so spending them on
the CLI makes both rows read "Could not check" on a busy host that holds both
grants. This module imports only the driver seam, under a second, and asks the same
:meth:`~kiro_crew.computer_use.backend.ComputerUseBackend.probe_permissions` the
doctor asks, so the two can never disagree about a host.

Keep it that way: ``test_computer_use_permission_probe.py`` fails if running it
loads ``kiro_crew.cli`` or the spec writer ``kiro_crew.agent``.
"""

from __future__ import annotations

import json
import logging
import sys

from kiro_crew.computer_use.permissions import (
    KEY_ACCESSIBILITY,
    KEY_RESPONSIBLE_HINT,
    KEY_SCREEN_RECORDING,
)
from kiro_crew.computer_use.types import PERMISSION_UNKNOWN

logger = logging.getLogger(__name__)


def probe_payload() -> dict[str, str]:
    """The current platform driver's permission block. ADVISORY. Never raises.

    A driver that cannot be built or probed answers ``unknown`` for both grants,
    never ``missing``: the rows are hints, and claiming a grant is missing when the
    probe merely failed would send the operator to System Settings for nothing.
    """
    from kiro_crew.computer_use.backend import get_shared_backend

    try:
        probe = get_shared_backend().probe_permissions()
    except Exception:
        logger.debug("computer-use permission probe failed", exc_info=True)
        return {
            KEY_ACCESSIBILITY: PERMISSION_UNKNOWN,
            KEY_SCREEN_RECORDING: PERMISSION_UNKNOWN,
            KEY_RESPONSIBLE_HINT: "",
        }
    return {
        KEY_ACCESSIBILITY: probe.accessibility,
        KEY_SCREEN_RECORDING: probe.screen_recording,
        KEY_RESPONSIBLE_HINT: probe.responsible_hint,
    }


def main() -> int:
    """Print the permission block as JSON. Exits 0 whatever the probe answered."""
    print(json.dumps(probe_payload()))
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry point
    sys.exit(main())
