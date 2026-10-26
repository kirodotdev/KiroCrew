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

The second half serves the RFC appendix "Red" rows, the controls governance cannot
reach: :func:`locked_off` (off in an enterprise build), :func:`follows_policy` (for
a row the RFC writes as "follows ``<key>``; off by default") and the two sandbox
floors, so local config cannot lower the sandbox.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict

from kiro_crew import edition

logger = logging.getLogger(__name__)

#: The reason a setting API reports for a local value an enterprise build ignores.
LOCKED_BY_POLICY = "locked by policy"

#: Control id -> the local source that is ignored and what the feature does instead.
#: Ids follow the RFC appendix "Crew key" column.
UNKEYED_LOCKS: Dict[str, str] = {
    "computer_use": "the computer_use.json opt-in is ignored; desktop control stays off",
    "tunnel": "tunnel.enabled is ignored; no tunnel is started",
    "telemetry.otlp_hosts": "telemetry.otlp_endpoint is ignored; no metrics leave the host",
    # Named for the RFC "developer_mode" policy term; the key that replaces this row
    # is separate work.
    "developer_mode": "KIROCREW_DEV_MODE is ignored; developer-only features stay off",
}

#: config.json path -> the control whose lock makes that local value inert.
CONFIG_PATH_LOCKS: Dict[str, str] = {
    "tunnel.enabled": "tunnel",
    "slack.use_tunnel_url": "tunnel",
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


# ── Red controls (RFC appendix "Red") ──

#: Said by a refused action, so the reason names the build rather than a setting.
LOCKED_REASON = "turned off in the enterprise edition"

#: Said when a "follows" row is refused because no policy turned its key on.
UNNAMED_REASON = "off by default in the enterprise edition until a policy enables it"

#: Decision layers that carry a policy answer (``evaluator.resolve``). ``default``
#: (nothing governs the scope) and ``profile`` (a profile alone) are not.
_POLICY_LAYERS = frozenset({"policy", "both"})


def locked_off() -> bool:
    """True when this build hardcodes the Red control off."""
    return edition.is_enterprise()


def follows_policy(decision: object) -> bool:
    """Whether a PERMITTING *decision* may stand for a "follows ``<key>``" row.

    Always true in an ``oss`` build. In an enterprise build only a permit the policy
    layer gave counts, so the feature is off by default and follows its key once a
    policy names it. Which policy tier may widen a key is the RFC's D3 work, not this.
    """
    if not locked_off():
        return True
    return getattr(decision, "layer", "") in _POLICY_LAYERS


def sandbox_mode_floor(mode: Any, default: Any) -> Any:
    """``agent.sandbox`` as read, except an enterprise build cannot read ``off``.

    There ``off`` reads as the shipped *default*, so local config cannot go below
    Crew's own sandbox; ``strict`` still narrows (Red "Minimum sandbox level"). An
    ``oss`` build returns *mode* unchanged. Read by
    ``sandbox_tiers._clamp_sandbox_mode_to_floor``, the clamp every spawn path shares.
    """
    if mode == "off" and locked_off():
        return default
    return mode


def unsandboxed_exec_floor(allow: bool, platform_default: bool) -> bool:
    """``sandbox_allow_unsandboxed_exec`` as read; an enterprise build cannot widen it.

    There a declared ``true`` reads as *platform_default* (fail-closed where a
    backend can exist), for the same reason as :func:`sandbox_mode_floor`; a
    declared ``false`` still narrows. An ``oss`` build returns *allow* unchanged.
    Read by ``sandbox_unavailable._allow_unsandboxed_exec``, the one gate.
    """
    if allow and locked_off():
        return platform_default
    return allow
