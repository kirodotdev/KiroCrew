"""Live identity for the CLI's home-scoped gateway mode decision."""

import sys
import time

from kiro_crew import platform_compat
from kiro_crew.config.paths import config_dir
from kiro_crew.gateway_lock import LockProbeError, lock_holder
from kiro_crew.instances import run_marker

# The clock and the sleep, named so a test can drive the wait without sleeping.
_now = time.monotonic
_sleep = time.sleep


def home_gateway_identity() -> tuple[int, str] | None:
    """Return this home's live identity, None if absent; indeterminate raises.

    The lock identifies this home's gateway even without a TCP listener. A
    listener's argv alone can name a gateway of another home on the same port.
    """
    holder = lock_holder(config_dir())
    if holder.pid is None or not holder.alive:
        return None
    return holder.pid, run_marker.pid_start_token(holder.pid)


def _unprobed_headless(data: dict[str, object] | None) -> bool:
    """Keep the validated record on an indeterminate probe, not proven absence.

    Restart's later stop and spawn guards still refuse an unresolved lock.
    The pure rule validates the record; its refusal means it recorded headless.
    """
    headless, _ = run_marker.gateway_mode_decision(data, None)
    return headless is not False


def restart_gateway_headless(port: int, interval: float) -> bool:
    """Settle the foreground mode before stop; refuse an unnamed boot at the bound."""
    deadline = _now() + run_marker.GATEWAY_MODE_WRITE_TIMEOUT_SECS
    try:
        while True:
            try:
                identity = home_gateway_identity()
            except LockProbeError:
                return _unprobed_headless(run_marker.read_gateway_mode())
            listening = (
                identity is not None
                and platform_compat.listening_pid_tool_available()
                and identity[0] in platform_compat.find_listening_pids(port)
            )
            # Read AFTER the listener probe: a headless boot may publish and bind
            # during that probe, so an earlier record cannot prove it ordinary.
            data = run_marker.read_gateway_mode()
            _, stale = run_marker.gateway_mode_decision(data, identity)
            if identity is None or (data is not None and not stale) or listening:
                return run_marker.gateway_headless_from(data, identity)
            remaining = deadline - _now()
            if remaining <= 0:
                break
            _sleep(min(interval, remaining))
        message = (
            "❌ The gateway of this data home is still starting and its mode cannot be "
            "checked; restart refused. Stop it with kirocrew stop, then start it with "
            "kirocrew gateway --headless or kirocrew gateway for one with the dashboard."
        )
    except run_marker.UnverifiedHeadlessMode:
        message = (
            "❌ No gateway of this data home is running; the last recorded mode is "
            "headless and cannot be checked; restart refused. Start it with "
            "kirocrew gateway --headless or kirocrew gateway for one with the dashboard."
        )
    except (OSError, ValueError):
        message = (
            "❌ Cannot read the gateway launch mode; restart refused. "
            "After stopping it yourself, start it with kirocrew gateway --headless "
            "if it must stay headless."
        )
    print(message, file=sys.stderr)
    sys.exit(1)


def service_gateway_headless(bound_secs: float, interval: float) -> bool:
    """Choose output after a service restart, waiting for the writer's identity.

    A replacement takes the lock before publishing its record. Until a live
    holder exists and the record names it, poll up to bound_secs. At the bound,
    an unverified headless record raises so the caller prints no access link.
    A damaged or unreadable record raises at once.
    """
    deadline = _now() + bound_secs
    while True:
        try:
            identity = home_gateway_identity()
        except LockProbeError:
            headless = _unprobed_headless(run_marker.read_gateway_mode())
            if _now() >= deadline:
                return headless
        else:
            data = run_marker.read_gateway_mode()
            _, stale = run_marker.gateway_mode_decision(data, identity)
            if (data is not None and identity is not None and not stale) or _now() >= deadline:
                return run_marker.gateway_headless_from(data, identity)
        _sleep(interval)
