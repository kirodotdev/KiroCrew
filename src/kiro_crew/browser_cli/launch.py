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

**Session naming lives here too.** The CLI addresses browsers by session name,
which is the other half of "which browser does a command reach"; keeping both
variables in one module is what stops the engine and the session from being
configured through unrelated seams.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from collections.abc import Mapping
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

#: The variable `playwright-cli` reads to decide which browser session a command
#: addresses. Its name is fixed by the CLI, not by us.
SESSION_ENV = "PLAYWRIGHT_CLI_SESSION"

#: Marks a generated name as Kiro Crew's in ``playwright-cli list``, so an
#: operator can tell an agent's browser from one they opened themselves, and so
#: :mod:`kiro_crew.browser_cli.reap` can tell which names are ours to reclaim.
SESSION_PREFIX = "kc-"

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


def browser_session_env(env: Mapping[str, str]) -> dict[str, str]:
    """Environment additions giving one agent process its own browser session.

    The CLI addresses browsers by session NAME and resolves a command with no
    name to the literal ``default``, so two agent processes that both run a bare
    command drive the SAME browser: one navigates the other's page out from
    under it, and either one's ``close`` leaves the other answering ``The
    browser 'default' is not open``. A distinct name per process removes the
    sharing without the agent having to remember ``-s=`` on every command.

    The name is random rather than derived from the session key, because the
    warm pool spawns a process BEFORE any session claims it: a key-derived name
    would be wrong for exactly the sessions the pool serves, and handing a
    per-session value to the spawn as ``extra_env`` would disqualify every
    session from the pool (a non-empty ``extra_env`` is a pool bypass in
    :meth:`kiro_crew.session.SessionManager.get_or_create`). Uniqueness per
    process is the entire requirement; legibility is served by the prefix.

    Empty when the variable is already set, the same override doctrine as
    :func:`cli_env_overrides`: an operator who named a session means one
    specific browser. Every process then shares that name, which is theirs to
    choose — including ``attach``-style workflows that need a fixed name.
    """
    if env.get(SESSION_ENV, "").strip():
        return {}
    return {SESSION_ENV: f"{SESSION_PREFIX}{uuid.uuid4().hex[:8]}"}
