"""Reclaims the browser a dead agent process left open.

A browser session outlives the command that opened it — that is what makes the
CLI usable across an agent's turns — but it also means the browser survives the
agent process itself. When a session's process is recycled (an RSS recycle, a
compaction fallback, a crash) its browser stays alive under a name nothing
addresses any more, holding a Chromium's worth of memory until something closes
it. Nothing else reclaims it: the orphan sweep in :mod:`kiro_crew.session_pid`
identifies MCP launcher shapes (``@playwright/mcp``, ``mcp start-server``), and a
``playwright-cli`` daemon matches neither.

**Liveness-keyed, never age-keyed**, and reclaimed by a SWEEP rather than a
teardown hook — the same doctrine as :mod:`kiro_crew.agent_scratch`, for the same
reason: a hard kill runs no teardown, so a hook would miss exactly the crash case
that leaks. The spawn records ``name -> owner pid``; the sweep releases a name
whose owner's process group is gone.

**An attached browser is released, never closed.** If the agent ran ``attach``,
the name is bound to the operator's own browser and ``close`` would take their
windows down with it. ``list --json`` reports ``attached`` per session, so an
attached one gets ``detach`` (releases the session, leaves the window) and only a
browser Kiro Crew launched gets ``close``.
"""

from __future__ import annotations

import json
import logging
import subprocess
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write
from kiro_crew.browser_cli.install import cli_env, cli_path
from kiro_crew.browser_cli.launch import SESSION_PREFIX
from kiro_crew.config.paths import config_dir

logger = logging.getLogger(__name__)

_REGISTRY_FILE = "browser-sessions.json"

#: A released browser is idle by definition, so the CLI calls are bounded rather
#: than patient: a hung daemon must not stall the sweep that follows it.
_CLI_TIMEOUT_S = 20.0


def registry_path() -> Any:
    """Where ``name -> owner pid`` lives (under the data home, like the config)."""
    return config_dir() / _REGISTRY_FILE


def _read() -> dict[str, int]:
    try:
        raw = json.loads(registry_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, int)}


def _write(entries: dict[str, int]) -> None:
    try:
        atomic_write(registry_path(), json.dumps(entries, indent=2) + "\n")
    except OSError:
        logger.debug("browser-reap: could not write the session registry", exc_info=True)


def record_session(name: str, pid: int) -> None:
    """Record that *pid* owns browser session *name*.

    Ignores a name Kiro Crew did not generate: an operator-set
    ``PLAYWRIGHT_CLI_SESSION`` names THEIR browser, and this module must never
    acquire a claim on it. Fail-open — an unrecorded name simply is not swept,
    and a recorder that runs on the spawn path must never be able to break one,
    so an unusable pid is dropped rather than raising.
    """
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return
    if not name.startswith(SESSION_PREFIX):
        return
    entries = _read()
    entries[name] = pid
    _write(entries)


def _cli_json(cli: str, *args: str) -> Any:
    out = subprocess.run(
        [cli, *args],
        env=cli_env(),
        capture_output=True,
        text=True,
        timeout=_CLI_TIMEOUT_S,
    )
    try:
        return json.loads(out.stdout)
    except ValueError:
        return None


def _live_sessions(cli: str) -> dict[str, bool]:
    """``name -> attached`` for every browser the CLI currently knows about."""
    payload = _cli_json(cli, "list", "--json")
    browsers = payload.get("browsers") if isinstance(payload, dict) else None
    if not isinstance(browsers, list):
        return {}
    live = {}
    for entry in browsers:
        if isinstance(entry, dict) and isinstance(entry.get("name"), str):
            live[entry["name"]] = bool(entry.get("attached"))
    return live


def _release(cli: str, name: str, attached: bool) -> None:
    verb = "detach" if attached else "close"
    subprocess.run(
        [cli, f"-s={name}", verb],
        env=cli_env(),
        capture_output=True,
        text=True,
        timeout=_CLI_TIMEOUT_S,
    )
    logger.info("browser-reap: %sed orphaned browser session %s", verb, name)


def sweep_dead_sessions() -> int:
    """Release every recorded browser whose owner process group is gone.

    Returns the number of sessions released. Blocking (subprocess calls) — run it
    off the event loop. Fail-open throughout: a browser left behind costs memory,
    while an exception escaping a maintenance sweep costs the task that runs it.
    """
    entries = _read()
    if not entries:
        return 0
    alive = {n: p for n, p in entries.items() if platform_compat.pgroup_exists(p)}
    dead = {n: p for n, p in entries.items() if n not in alive}
    if not dead:
        return 0

    cli = cli_path()
    if cli is None:
        # Without the CLI nothing can be released, and dropping the entries would
        # lose the only record of what to release once it is installed again.
        return 0

    released = 0
    try:
        live = _live_sessions(cli)
    except (OSError, subprocess.SubprocessError):
        logger.debug("browser-reap: could not list browser sessions", exc_info=True)
        return 0
    for name in dead:
        if name not in live:
            # Already gone: forget it rather than shelling out for nothing.
            continue
        try:
            _release(cli, name, live[name])
            released += 1
        except (OSError, subprocess.SubprocessError):
            logger.debug("browser-reap: could not release %s", name, exc_info=True)
            alive[name] = entries[name]  # keep the claim; retry next sweep
    _write(alive)
    return released
