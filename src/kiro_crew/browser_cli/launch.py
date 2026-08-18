"""The launch config that makes `playwright-cli` open the browser Kiro Crew provisions.

Kiro Crew installs, gates on, and offers downloads for **Chromium**:
``install-browser`` fetches the Chromium build, ``browser_ok`` is
``browsers_present()["chromium"]``, and ``attach --extension`` supports that
family alone. The CLI's own default is a different browser -- the branded Chrome
*channel*, an OS-level install at a path like ``/opt/google/chrome/chrome`` that
Kiro Crew never provisions and cannot install without root. So on a host that has
done everything the product asked, the first browse fails with

    Chromium distribution 'chrome' is not found at /opt/google/chrome/chrome

while every readiness signal is honestly green: the Chromium build really is
downloaded. Selecting the engine is what closes that gap, and it is a
configuration fact rather than a defect in the readiness gate.

**Why a config file and not a flag or an env var.** All three exist; only the
file works for a whole session.

- ``--browser`` and ``PLAYWRIGHT_MCP_BROWSER`` take ``chrome, firefox, webkit,
  msedge``. Neither accepts ``chromium``, so neither can name the engine that is
  actually installed. Verified against the CLI's own help and env table.
- ``--config`` is accepted only on the session-establishing commands (``open``,
  ``attach``) and is rejected by the follow-up commands that make up most of a
  session, the same constraint that makes
  :func:`kiro_crew.browser_cli.snapshots.cli_env_overrides` use an env var.
- ``PLAYWRIGHT_MCP_CONFIG`` names a config FILE and applies to every invocation
  uniformly. That is the one channel that reaches a command line Kiro Crew never
  constructs, which is the whole shape of this capability: the agent runs the CLI
  as a shell command.

The config schema is nested under a ``browser`` key
(``{"browser": {"browserName": ...}}``); a flat ``browserName`` at the top level
parses without error and selects nothing.

**The browser sandbox is left alone.** Chromium's own sandbox is a security
boundary, so this module never writes ``chromiumSandbox``. A host that cannot
run it -- a container without the needed kernel permissions -- needs an operator
decision, not a default that quietly removes a boundary for everyone. That is
what :func:`cli_env_overrides` deferring to an operator-set variable is for.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import config_dir

logger = logging.getLogger(__name__)

#: The variable `playwright-cli` reads to locate its config file. Its name is
#: fixed by the CLI, not by us.
CONFIG_ENV = "PLAYWRIGHT_MCP_CONFIG"

#: The engine Kiro Crew provisions and gates on. Kept as one named constant so
#: the config can never disagree with what ``install-browser`` fetched.
LAUNCH_ENGINE = "chromium"

_CONFIG_FILE = "playwright-cli-config.json"


def launch_config_path() -> Path:
    """Where the generated launch config lives.

    Under the data home, so it is a fixed absolute path independent of whichever
    working directory an agent turn ran in, and so an isolated ``KIROCREW_HOME``
    (a pod, a test) stays isolated here too.
    """
    return config_dir() / _CONFIG_FILE


def desired_config() -> dict[str, object]:
    """The config Kiro Crew generates.

    Deliberately minimal: it names the engine and nothing else. Every key added
    here becomes a default an operator has to discover in order to override, and
    the engine is the only one the product's own install flow already decided.
    """
    return {"browser": {"browserName": LAUNCH_ENGINE}}


def write_config() -> Path | None:
    """Write the launch config, returning its path (``None`` if it could not be written).

    Rewritten whenever it does not already match :func:`desired_config`, so a
    file left by an older version converges. Best-effort by contract: this runs
    on the gateway's startup path with nothing waiting on it, and a config that
    cannot be written must not stop the gateway from coming up -- browsing then
    behaves as it did before this file existed.
    """
    path = launch_config_path()
    payload = json.dumps(desired_config(), indent=2) + "\n"
    try:
        if path.is_file() and path.read_text(encoding="utf-8") == payload:
            return path
    except (OSError, UnicodeDecodeError):
        # Unreadable is not a reason to skip the write; it is a reason to do it.
        pass
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path, payload)
    except OSError:
        logger.warning(
            "could not write the browser launch config at %s; playwright-cli will "
            "fall back to its own default browser channel, which Kiro Crew does "
            "not install",
            path,
        )
        return None
    return path


def cli_env_overrides() -> dict[str, str]:
    """Environment additions pointing the CLI at :func:`launch_config_path`.

    Empty when :data:`CONFIG_ENV` is already set in the environment. An operator
    who named their own config file has made a deliberate choice -- a different
    engine, a pinned ``executablePath``, launch options for a container that
    cannot run the browser sandbox -- and silently replacing it would both
    override that choice and remove the escape hatch this module's narrow
    defaults depend on.

    Empty as well when the file could not be written, because pointing the CLI at
    a path that does not exist is worse than leaving it on its own default: the
    CLI fails on the missing config instead of on the missing browser, which is a
    strictly less diagnosable error for the same broken outcome.
    """
    if os.environ.get(CONFIG_ENV, "").strip():
        return {}
    path = write_config()
    return {CONFIG_ENV: str(path)} if path is not None else {}
