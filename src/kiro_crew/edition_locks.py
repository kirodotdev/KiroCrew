"""Local enables an enterprise build ignores, for controls with no governance key yet.

RFC ``rfc-crew-enterprise-edition`` decision D3: in an enterprise build only the
remote policy can turn a control on; local config, environment variables, profiles
and dashboard settings may only narrow it. A control that already has a governance
key gets that from the evaluator. The controls in :data:`UNKEYED_LOCKS` have no key
yet, so no remote rule can turn them on, and their local switch is ignored here: the
feature stays at its minimal set. A later change that adds a real key for one of them
replaces its row with a governance check.

Each feature calls :func:`local_enable_locked` where it reads its own switch. In the
``oss`` edition it returns ``False`` and nothing changes. In an enterprise build it
returns ``True`` and logs once per control. A setting API reports a locked control
with :data:`LOCKED_BY_POLICY`.
"""

from __future__ import annotations

import logging
import threading
from typing import Dict

logger = logging.getLogger(__name__)

#: The reason a setting API reports for a local value an enterprise build ignores.
LOCKED_BY_POLICY = "locked by policy"

#: Control id -> the local source that is ignored and what the feature does instead.
#: Ids follow the RFC appendix "Crew key" column.
UNKEYED_LOCKS: Dict[str, str] = {
    "app_dev_mode": "app dev mode toggles are ignored; every app is served as installed",
    "updates.channel": "the release channel file is ignored; the install follows stable",
    "computer_use": "the computer_use.json opt-in is ignored; desktop control stays off",
    "tunnel": "tunnel.enabled is ignored; no tunnel is started",
    "telemetry.otlp_hosts": "telemetry.otlp_endpoint is ignored; no metrics leave the host",
}

#: config.json path -> the control whose lock makes that local value inert.
CONFIG_PATH_LOCKS: Dict[str, str] = {
    "tunnel.enabled": "tunnel",
    "telemetry.otlp_endpoint": "telemetry.otlp_hosts",
}

_logged: "set[str]" = set()
_logged_lock = threading.Lock()


def local_enable_locked(control: str) -> bool:
    """True when this build ignores the local switch for *control*.

    Raises ``KeyError`` for a control not in :data:`UNKEYED_LOCKS`, so a misspelled
    id fails in tests instead of silently reading as unlocked.
    """
    reason = UNKEYED_LOCKS[control]
    from kiro_crew.edition import is_enterprise

    if not is_enterprise():
        return False
    with _logged_lock:
        first = control not in _logged
        _logged.add(control)
    if first:
        logger.warning("enterprise build: %s (%s)", reason, LOCKED_BY_POLICY)
    return True


def locked_controls() -> "tuple[str, ...]":
    """Every control this build locks, for a setting API to report. Empty on oss."""
    from kiro_crew.edition import is_enterprise

    return tuple(sorted(UNKEYED_LOCKS)) if is_enterprise() else ()


def config_path_locked(path: str) -> bool:
    """True when this build ignores the local value at config *path*. Never logs."""
    from kiro_crew.edition import is_enterprise

    return path in CONFIG_PATH_LOCKS and is_enterprise()


def reset_logged() -> None:
    """Forget which locks have logged.  Test helper."""
    with _logged_lock:
        _logged.clear()
