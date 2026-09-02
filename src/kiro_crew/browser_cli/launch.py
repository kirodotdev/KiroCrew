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

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write
from kiro_crew.browser_cli.install import cli_lifecycle_env_supported
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

#: The variable playwright-core reads before ``os.tmpdir()`` when choosing the
#: daemon control-socket root. Its name is upstream's test-prefixed public seam,
#: but it is the only supported way to keep the socket reachable after an
#: agent's per-process scratch directory is reclaimed.
SOCKETS_ENV = "PWTEST_SOCKETS_DIR"

#: The registry root holding ``<workspace-hash>/<session>.session`` metadata.
#: Fixed beside the socket root so controlled teardown can locate the socket
#: without executing the user-writable playwright-cli wrapper.
DAEMON_DIR_ENV = "PWTEST_DAEMON_SESSION_DIR"

#: Short common root; each generated session gets its own `/<8hex>/s` and
#: `/<8hex>/d` subtree so `playwright-cli list` cannot enumerate peer chats.
_LIFECYCLE_DIR = "pw"
_UNIX_SOCKET_PATH_MAX_BYTES = 103

#: Marks a generated name as Kiro Crew's in ``playwright-cli list``, so an
#: operator can tell an agent's browser from one they opened themselves.
_SESSION_PREFIX = "kc-"

_CONFIG_FILE = "playwright-cli-config.json"


#: Leaf names :func:`socket_dir` and :func:`daemon_dir` build under a session dir.
_LIFECYCLE_LEAVES = frozenset({"s", "d"})


def _is_generated_leaf(leaf: str) -> bool:
    """Whether *leaf* is the 8-hex filesystem leaf of a generated session."""
    return len(leaf) == 8 and all(c in "0123456789abcdef" for c in leaf)


def _session_leaf(session_name: str) -> str:
    """Filesystem leaf for one generated ``kc-<8hex>`` session."""
    if not session_name.startswith(_SESSION_PREFIX):
        return ""
    leaf = session_name.removeprefix(_SESSION_PREFIX)
    return leaf if _is_generated_leaf(leaf) else ""


def socket_dir(session_name: str, base: Path | None = None) -> Path:
    """Stable, per-session daemon-socket root outside agent scratch."""
    root = base if base is not None else config_dir() / _LIFECYCLE_DIR
    return root / _session_leaf(session_name) / "s"


def daemon_dir(session_name: str, base: Path | None = None) -> Path:
    """Stable, per-session Playwright registry root outside agent scratch."""
    root = base if base is not None else config_dir() / _LIFECYCLE_DIR
    return root / _session_leaf(session_name) / "d"


def _operator_base(configured: str) -> Path | None:
    """An operator-chosen lifecycle root, or ``None`` to use the default.

    ``None`` for an unset value AND for one of our own roots arriving by
    INHERITANCE, which is the same distinction :func:`browser_session_env`
    draws on the session name -- ours-by-inheritance is regenerated, only a
    foreign value is honoured.

    Both spawn sites build the child environment from ``{**os.environ, ...}``,
    so a gateway started from inside an agent process hands its own
    ``SOCKETS_ENV`` down. Treating that as an operator base namespaced EVERY
    session it hosts one level deeper inside the parent's root, recursively:
    measured 133 children under a single root and a depth of four, with real
    socket paths at 79 of the ``_UNIX_SOCKET_PATH_MAX_BYTES`` budget. Nesting
    spends ~12 bytes a level, so it walks into the AF_UNIX ceiling this module
    already guards -- and that guard returns ``{}``, which drops the daemon back
    under reclaimable scratch and reinstates the unreachability the roots exist
    to prevent.

    Recognition is by SHAPE -- a ``<root>/<8hex>/{s,d}`` tail, which is what
    :func:`socket_dir` and :func:`daemon_dir` build -- not by location under the
    current ``config_dir()``. Every trigger flow changes the data home
    (``dev-backend.sh`` exports ``KIROCREW_HOME``, and a pod runs an isolated
    one), so an inherited root sits under the PARENT's home and a location test
    would read it as foreign and keep nesting. Shape is also why the sibling
    ``kc-`` prefix guard survives crossing installations. The residual cost is
    an operator root that happens to end in ``<8hex>/s``, which is treated as
    ours; that is the same collision the reserved prefix already accepts.
    """
    if not configured:
        return None
    path = Path(configured)
    if path.name in _LIFECYCLE_LEAVES and _is_generated_leaf(path.parent.name):
        return None
    return path


def browser_socket_env(env: Mapping[str, str]) -> dict[str, str]:
    """Environment additions keeping daemon sockets reachable and discoverable.

    ``playwright-cli`` launches its daemon after Kiro Crew has pointed
    ``TMPDIR`` at a per-process scratch directory. Without ``SOCKETS_ENV`` the
    socket disappears when that scratch is reclaimed. ``DAEMON_DIR_ENV`` fixes
    the corresponding session registry location, letting Kiro Crew find and
    validate the exact generated session file at controlled teardown without
    executing the user-writable CLI wrapper.

    This helper is called only when Kiro Crew generated ``SESSION_ENV``.
    A location variable holding a FOREIGN value is treated as an
    operator-selected BASE root and namespaced by the generated session; one
    holding a value already under our own lifecycle root arrived by inheritance
    and is ignored in favour of that root, so namespaces stay siblings instead
    of nesting (see :func:`_operator_base`). Non-generated operator sessions
    never call this helper. Both final directories are owner-restricted. If
    validation or preparation fails, no partial additions are returned and the
    current TMPDIR/default-registry behavior remains. This helper performs
    filesystem I/O and event-loop callers MUST offload it.
    """
    session_name = env.get(SESSION_ENV, "").strip()
    if not _session_leaf(session_name):
        return {}
    if not cli_lifecycle_env_supported():
        logger.warning(
            "installed playwright-cli does not expose the stable daemon "
            "socket/session hooks; leaving its lifecycle environment unchanged"
        )
        return {}
    configured_sockets = env.get(SOCKETS_ENV, "").strip()
    configured_daemons = env.get(DAEMON_DIR_ENV, "").strip()
    if (
        configured_sockets
        and not Path(configured_sockets).is_absolute()
        or configured_daemons
        and not Path(configured_daemons).is_absolute()
    ):
        logger.warning("Playwright lifecycle root overrides must be absolute paths")
        return {}
    sockets_path = socket_dir(session_name, _operator_base(configured_sockets))
    daemons_path = daemon_dir(session_name, _operator_base(configured_daemons))
    additions: dict[str, str] = {}
    # Upstream builds `<root>/cli/<16-char-workspace>-<11-char-session>.sock`.
    # Check the complete shortest non-trimmed form; if even that cannot fit,
    # makeSocketPath raises and browsing fails before cleanup can help.
    worst_case = sockets_path / "cli" / "0000000000000000-kc-00000000.sock"
    if (
        not platform_compat.IS_WINDOWS
        and len(os.fsencode(str(worst_case))) > _UNIX_SOCKET_PATH_MAX_BYTES
    ):
        logger.warning(
            "Playwright socket directory is too long for AF_UNIX (%d bytes): %s",
            len(os.fsencode(str(worst_case))),
            sockets_path,
        )
        return {}
    for key, path in ((SOCKETS_ENV, sockets_path), (DAEMON_DIR_ENV, daemons_path)):
        try:
            platform_compat.make_owner_only_dir(path)
            platform_compat.restrict_dir_to_owner(path)
        except OSError:
            logger.warning(
                "could not prepare Playwright lifecycle directory at %s; "
                "browser daemon cleanup may be unavailable",
                path,
            )
            return {}
        additions[key] = str(path)
    return additions


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

    Empty when the variable is already set to a name we did not generate, the
    same override doctrine as :func:`cli_env_overrides`: an operator who named a
    session means one specific browser. Every process then shares that name,
    which is theirs to choose — including ``attach``-style workflows that need a
    fixed name. The ``kc-`` prefix is RESERVED for that reason: an operator who
    wants a fixed shared name must not use it.

    A value carrying that prefix is regenerated rather than preserved, because
    it is one of ours arriving by INHERITANCE, not by intent. Both spawn paths
    build the child env as ``{**os.environ}``, so a gateway started from inside
    an agent process — which this repo's own ``kirocrew-worktree-dev`` skill
    tells an agent to do via ``./dev-backend.sh`` — would pass its caller's
    generated name down to every session it hosts. Preserving it there would
    put every chat on that gateway back on one shared browser and silently
    no-op the isolation this function exists to provide.
    """
    existing = env.get(SESSION_ENV, "").strip()
    if existing and not existing.startswith(_SESSION_PREFIX):
        return {}
    return {SESSION_ENV: f"{_SESSION_PREFIX}{uuid.uuid4().hex[:8]}"}
