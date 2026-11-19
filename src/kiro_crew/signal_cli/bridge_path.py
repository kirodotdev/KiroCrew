"""Where the ``signal-cli`` binary is allowed to come from.

The channel spawns a child process that holds the Signal account's keys, so
*who chooses that executable* is a security boundary, and the rule is the
iMessage bridge's (see ``imessage/bridge_path.py``): **nothing an agent can
write may influence which binary runs.**

* **Not configuration.** ``config.json`` is agent-writable, so a ``cli_path``
  setting would let an auto-approved agent shell name a payload that the
  gateway then executes outside the agent sandbox on the next restart.
* **Not ``PATH``.** ``~/.local/bin`` is writable and usually on it, so a
  ``PATH`` lookup is the same hole by a different route.

Resolution is the environment variable :data:`ENV_OVERRIDE` first, then a fixed
source-level list of install locations. The environment is the operator's: it
is set where the gateway process is launched (a shell profile, a unit file, a
launch agent), which no tool call reaches. signal-cli has no single canonical
install prefix -- distro packages, Homebrew, the upstream tarball under
``/opt`` and a snap all differ -- so the override is what keeps a working
install from being rejected merely for living somewhere unlisted.
"""

from __future__ import annotations

import os
from pathlib import Path

BRIDGE_BINARY = "signal-cli"

#: Operator-controlled override: an ABSOLUTE path to the signal-cli executable.
ENV_OVERRIDE = "KIROCREW_SIGNAL_CLI"

# Standard install locations, tried in order after the override. Absolute and
# fixed at source level: an entry here is a reviewer's decision. They are POSIX
# prefixes only; Windows has no standard signal-cli location, so there the
# channel resolves through ENV_OVERRIDE alone.
TRUSTED_BRIDGE_PATHS: tuple[str, ...] = (
    "/usr/local/bin/signal-cli",
    "/usr/bin/signal-cli",
    "/opt/homebrew/bin/signal-cli",
    "/opt/signal-cli/bin/signal-cli",
    "/snap/bin/signal-cli",
)


def _usable(candidate: str) -> bool:
    try:
        path = Path(candidate)
        return path.is_absolute() and path.is_file() and os.access(path, os.X_OK)
    except OSError:
        # An unstattable candidate is simply not a hit.
        return False


def resolve_bridge_path() -> str:
    """Return the signal-cli executable to spawn, or ``""`` when none is usable.

    A set but unusable override (relative, missing, not executable) is NOT
    silently replaced by a listed location: the operator named one binary, and
    running a different one would be a surprise. It resolves to ``""`` so the
    readiness surface reports the channel as needing setup.
    """
    override = os.environ.get(ENV_OVERRIDE, "").strip()
    if override:
        return override if _usable(override) else ""
    for candidate in TRUSTED_BRIDGE_PATHS:
        if _usable(candidate):
            return candidate
    return ""


def describe_lookup() -> str:
    """Where :func:`resolve_bridge_path` looked, for a "not installed" error."""
    override = os.environ.get(ENV_OVERRIDE, "").strip()
    if override:
        return f"{ENV_OVERRIDE}={override!r} is not an absolute path to an executable file"
    return f"looked in {', '.join(TRUSTED_BRIDGE_PATHS)}; or set {ENV_OVERRIDE}"
