"""Keep an MCP server's ``env`` on this machine when its spec is projected onto KAS.

On KAS an agent's MCP servers travel to the engine in ``session/new``
(``_meta.kiro.customAgents``), and the engine stores that batch verbatim in the
session's ``session.json``. So :func:`kiro_crew.acp.kas_agents._withhold_credential_fields`
keeps env values off the wire. A stdio server that needs one of them would start
without it: it fails to start and is missing from the tool list, or runs without
its setting. On kiro-cli v3 the engine reads the agent file from disk, so the env
never enters the session record there.

This module gives the server its values without putting one on the wire. For one stdio server
of one session it writes a LAUNCH SNAPSHOT -- the server's command, args and the
env values the wire withheld, nothing else of the agent -- into
:data:`SNAPSHOT_LEAF` under the crew data home, owner-only, and hands KAS a fixed
launcher instead (:mod:`kiro_crew.kas_env_shim`) with a reference that carries no
secret: the session key, the agent, the server name and the snapshot's version.
The launcher derives the snapshot's file name itself from that reference, checks
that the file names the same session, agent, server and version, and only then
execs the real command with those values added to its environment.

The leaf is sandbox-READONLY (the KAS child reads it, a sandboxed write is
refused) and on the agent file tools' read+write floor
(``security.paths_lists``), the treatment ``pi-gate`` gets. A sandboxed shell can
still read it; the same values are in the user's own agent file, which that shell
could already read, so no new reader gains them.

Every write gets a new random version, and the version is part of the file name,
so each projection (new, resume, reload) writes files no other projection shares,
even another provider's of the same session. Whoever holds the projected batch
removes exactly the files it references when it shuts down
(:func:`discard_batch`); there is no shared registry to keep in step. A snapshot
nobody removed (a provider that never shut down, a batch a reload replaced) stays
until the next gateway boot, whose background sweep removes those last written
:data:`_STALE_AFTER_SECS` before that boot (:func:`sweep_stale`). Blocking file I/O throughout;
callers run it off the event loop.
"""

from __future__ import annotations

import json
import logging
import secrets
from pathlib import Path
from typing import Any, Mapping

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write
from kiro_crew.kas_env_shim import (
    SHIM_MODULE,
    SNAPSHOT_FORMAT,
    SNAPSHOT_LEAF,
    SNAPSHOT_MARKER,
    snapshot_name,
)

logger = logging.getLogger(__name__)

#: The boot sweep removes a snapshot last written this long before boot. The
#: previous run's agent processes are already ended by then, so the age only
#: protects a second gateway that shares this data home and still runs sessions.
_STALE_AFTER_SECS = 7 * 24 * 3600


def snapshot_dir() -> Path:
    """The owner-only directory snapshots live in, under the crew data home."""
    from kiro_crew.config.paths import config_dir

    return config_dir() / SNAPSHOT_LEAF


def launchable(entry: Mapping[str, Any]) -> bool:
    """Whether a projected server entry is one the launcher can start: stdio only.

    A remote server (``url``) has no process to start here, so its withheld
    values stay withheld.
    """
    command = entry.get("command")
    return isinstance(command, str) and bool(command.strip()) and not entry.get("url")


def withheld_env(original: Any, projected_env: Any) -> dict[str, str]:
    """The env values of *original* that the wire filter did not keep, as strings.

    A process environment holds strings, so a JSON number or boolean is written
    the way a shell would spell it (``8080``, ``true``); a value with no such
    spelling (an object, a list, ``null``) is left out and stays withheld. A value
    the filter KEPT (a relayed ``${NAME}`` reference, a boolean flag) stays on the
    entry so the engine treats it exactly as before.
    """
    if not isinstance(original, dict):
        return {}
    kept = projected_env if isinstance(projected_env, dict) else {}
    out: dict[str, str] = {}
    for k, v in original.items():
        if not isinstance(k, str) or not k or k in kept:
            continue
        if isinstance(v, bool):
            out[k] = "true" if v else "false"
        elif isinstance(v, (str, int, float)):
            out[k] = str(v)
    return out


def launch_through_shim(
    projected: dict[str, Any],
    *,
    session_key: str,
    agent_id: str,
    server: str,
    env: Mapping[str, str],
) -> dict[str, Any]:
    """*projected* rewritten to start through the launcher, its snapshot written.

    *env* is what the wire withheld (:func:`withheld_env`); placeholders in it are
    expanded here, against the same filtered view the gateway's own sidecar uses,
    because the launcher runs with the engine's reduced environment. Raises
    ``OSError`` when the snapshot cannot be written; the caller then hands the
    engine a launcher reference that refuses, never the bare command.
    """
    from kiro_crew.mcp_gateway.rewriter_env import _expand_env_map

    args = projected.get("args")
    launch: dict[str, Any] = {
        SNAPSHOT_MARKER: SNAPSHOT_FORMAT,
        "session": session_key,
        "agent": agent_id,
        "server": server,
        "command": projected["command"],
        "args": [a for a in args if isinstance(a, str)] if isinstance(args, list) else [],
        "env": dict(_expand_env_map(dict(env), server=server)),
    }
    directory = snapshot_dir()
    platform_compat.make_owner_only_dir(directory)
    version = secrets.token_hex(16)
    name = snapshot_name(session_key, agent_id, server, version)
    # Owner-only before any byte is written, and it raises if that lockdown fails.
    atomic_write(
        directory / name,
        json.dumps({**launch, "version": version}, sort_keys=True).encode("utf-8"),
        fsync=True,
        restrict_to_owner=True,
    )
    return _shim_entry(projected, session_key, agent_id, server, version)


#: The version a refused launch carries. ``secrets.token_hex`` only yields hex, so
#: no written snapshot can ever be named with it, and the launcher refuses it.
_UNWRITTEN_VERSION = "unwritten"


def _shim_entry(
    projected: dict[str, Any], session_key: str, agent_id: str, server: str, version: str
) -> dict[str, Any]:
    """*projected* with its command replaced by the launcher and this reference."""
    data_home = str(snapshot_dir().parent)
    shim_argv = platform_compat.isolated_python_argv("-P", "-m", SHIM_MODULE)
    out = dict(projected)
    out["command"] = shim_argv[0]
    out["args"] = [
        *shim_argv[1:],
        "--session",
        session_key,
        "--agent",
        agent_id,
        "--server",
        server,
        "--version",
        version,
    ]
    # The data home is a path, not a secret, and the engine passes only its
    # default environment to a server, so the launcher learns it here.
    out["env"] = {**(projected.get("env") or {}), "KIROCREW_HOME": data_home}
    return out


def relay_withheld_env(
    projected: dict[str, Any],
    original_env: Any,
    *,
    session_key: str,
    agent_id: str,
    server: str,
) -> tuple[dict[str, Any], bool, bool]:
    """``(entry, launched, complete)``: *projected* through the launcher when the wire
    withheld env values it needs, else *projected* unchanged.

    *complete* says every withheld value went into the snapshot, so the caller's
    "not relaying env" log line has nothing left to name. When the snapshot cannot
    be written the entry still points at the launcher, with a reference no snapshot
    answers, so the server refuses to start rather than run without its env.
    """
    env = withheld_env(original_env, projected.get("env"))
    if not env or not session_key or not launchable(projected):
        return projected, False, False
    kept = projected.get("env") or {}
    every = {k for k in original_env if isinstance(k, str) and k and k not in kept}
    try:
        entry = launch_through_shim(
            projected, session_key=session_key, agent_id=agent_id, server=server, env=env
        )
    except OSError:
        logger.warning(
            "agent %r: could not write the local launch for MCP server %r; it will "
            "refuse to start rather than run without its env",
            agent_id,
            server,
            exc_info=True,
        )
        return (
            _shim_entry(projected, session_key, agent_id, server, _UNWRITTEN_VERSION),
            False,
            False,
        )
    return entry, True, every <= set(env)


def _remove(paths: list[Path]) -> None:
    for path in paths:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            logger.warning("could not remove KAS launch snapshot %s", path.name, exc_info=True)


def sweep_stale(boot_time: float) -> None:
    """Remove every snapshot last written :data:`_STALE_AFTER_SECS` before *boot_time*.

    The gateway starts it once, as a background task, with the time it took right
    after the previous run's agent processes were ended and before any session of
    this run existed (``slack.gateway_runtime.service_boot``). The cutoff is fixed
    to that moment, not to when the scan runs, so a snapshot this run writes is
    never older than it and is never removed, however late the task gets to it.
    It is how a snapshot no batch references any more (a resumed session's, a
    replaced batch's, a crashed run's) leaves the disk. Nothing else calls it,
    so one session's projection or shutdown never removes a file another live
    session's launcher still reads.
    """
    try:
        cutoff = boot_time - _STALE_AFTER_SECS
        stale: list[Path] = []
        for path in snapshot_dir().iterdir():
            try:
                if path.stat().st_mtime < cutoff:
                    stale.append(path)
            except FileNotFoundError:
                continue
        _remove(stale)
    except FileNotFoundError:
        return
    except OSError:
        # Pruning is housekeeping; a projection never fails for it.
        logger.warning("could not prune KAS launch snapshots", exc_info=True)


def _flag(args: list[Any], flag: str) -> str:
    try:
        value = args[args.index(flag) + 1]
    except (ValueError, IndexError):
        return ""
    return value if isinstance(value, str) else ""


def batch_snapshot_names(custom_agents: Any) -> list[str]:
    """The snapshot file names a projected ``customAgents`` batch references."""
    names: list[str] = []
    for agent in custom_agents if isinstance(custom_agents, list) else []:
        servers = agent.get("mcpServers") if isinstance(agent, dict) else None
        for entry in servers.values() if isinstance(servers, dict) else []:
            args = entry.get("args") if isinstance(entry, dict) else None
            if not isinstance(args, list) or SHIM_MODULE not in args:
                continue
            ref = [_flag(args, f) for f in ("--session", "--agent", "--server", "--version")]
            if all(ref):
                names.append(snapshot_name(*ref))
    return names


def discard_batch(custom_agents: Any, written: Any = ()) -> None:
    """Remove the snapshots its holder's projections wrote: the holder shut down.

    *written* is every name the session's projections wrote, read off each batch
    before ``session/load`` hoisted its servers out of the agent block
    (``AcpSessionHandle.kas_launch_snapshots``); *custom_agents* is the batch
    registered now. Both are needed: a hoisted launcher entry is absent from the
    batch, and an older handle's batch is absent from the provider.
    """
    try:
        directory = snapshot_dir()
        names = list(batch_snapshot_names(custom_agents))
        names += [n for n in written if isinstance(n, str)] if isinstance(written, list) else []
        _remove([directory / name for name in dict.fromkeys(names) if Path(name).name == name])
    except OSError:
        logger.warning("could not remove KAS launch snapshots", exc_info=True)
