"""Which MCP servers the latest default-agent rebuild left out, and why.

``resolve_mcp_servers`` drops a server whose declared command does not resolve on
the rebuild's PATH, and says so only in a log WARNING. A server the user switched
off is not counted: nothing would have started it. A user with no log open
then sees the server listed on the dashboard while the agent has none of its
tools, with nothing on screen to say why. This store is the durable copy of that
one fact, written after each rebuild that lands its spec and read by
``GET /api/mcp`` to put a plain reason on the server's row, by the
``agent_spec_dead_paths`` probe of ``diagnose_settings``, and by
``agent.ensure_agent_materialized``, which reruns the rebuild before a
default-agent session once a recorded command resolves again.

It also keeps a second fact of the same pass, under ``unused_own``: the user's own
server entries the agent does not run because a server the edition ships has the
same name (``mcp_sources.unused_own_entries``). The edition's command and
arguments win, so without this a deliberately different same-named server is left
out with no word anywhere. ``GET /api/mcp`` puts it on the row as
``ownEntryUnused`` and ``kirocrew doctor`` prints it in the MCP Tools section.

The file is replaced whole on every rebuild, so it always describes the LATEST
pass: a server whose program was installed, or that was removed, drops out of it
the next time the spec is rebuilt. It never changes what is mounted; the rebuild
decides that and this only reports it.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import data_home

logger = logging.getLogger(__name__)

STORE_FILENAME = "mcp-dropped-servers.json"
STORE_VERSION = 1

#: The declared command did not resolve on the rebuild's PATH. The only reason
#: recorded today; the field exists so the UI can key its wording on a class
#: rather than on the log text.
REASON_COMMAND_NOT_FOUND = "command_not_found"

# Bounds for a file the agent's own file tools can reach: a hand-edited store must
# not put an unbounded read or an unbounded string on the wire.
_STORE_MAX_BYTES = 1024 * 1024
_NAME_MAX_CHARS = 256
_COMMAND_MAX_CHARS = 1024


def store_path() -> Path:
    return data_home() / STORE_FILENAME


def record_rebuild_drops(
    unresolved_commands: dict[str, str],
    unused_own: dict[str, dict[str, str]] | None = None,
) -> None:
    """Replace the store with this rebuild's command-not-found drops and unused entries.

    *unused_own* is ``{name: {"file", "command"}}`` from
    ``mcp_sources.unused_own_entries``. Best effort: a failed write is logged and
    swallowed, because this is a report about the rebuild and must never fail the
    rebuild itself. An unchanged store is not rewritten, since the rebuild runs on
    every config change.
    """
    servers = {
        name: {"command": command, "reason": REASON_COMMAND_NOT_FOUND}
        for name, command in sorted(unresolved_commands.items())
    }
    unused = {
        name: {"file": rec["file"], "command": _shown_command(rec["command"])}
        for name, rec in sorted((unused_own or {}).items())
    }
    if servers == load_dropped() and unused == load_unused_own():
        return
    payload: dict[str, Any] = {"version": STORE_VERSION, "servers": servers}
    if unused:
        payload["unused_own"] = unused
    try:
        atomic_write(store_path(), json.dumps(payload, indent=2) + "\n")
    except OSError:
        logger.warning("could not record dropped MCP servers", exc_info=True)


def _shown_command(command: str) -> str:
    """*command*, or a ``url`` cut to its scheme and host.

    The record reaches ``GET /api/mcp`` (the MCP page, and any token allowed to read
    the inventory) and ``kirocrew doctor``, whose output gets pasted into bug
    reports. Every other part of a user's remote URL can carry a credential --
    userinfo, a query token, and a path segment some endpoints authenticate with --
    so only the scheme and host are stored (:func:`asset_downloader.redact_url`);
    the file the record names tells the user which entry it is.
    """
    if "://" not in command:
        return command
    from kiro_crew.asset_downloader import redact_url

    return redact_url(command)


def _load_section(key: str) -> dict[str, Any]:
    """The store's *key* map, or ``{}`` when the store or the map is unusable."""
    path = store_path()
    try:
        if path.stat().st_size > _STORE_MAX_BYTES:
            return {}
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, RecursionError):
        logger.debug("unreadable %s; treating it as empty", path, exc_info=True)
        return {}
    section = raw.get(key) if isinstance(raw, dict) else None
    return section if isinstance(section, dict) else {}


def load_dropped() -> dict[str, dict[str, str]]:
    """``{name: {"command", "reason"}}`` from the latest rebuild; ``{}`` if none.

    A missing, oversized, unreadable or malformed store reads as empty: the row
    then simply carries no drop note, which is what it showed before this store
    existed.
    """
    out: dict[str, dict[str, str]] = {}
    for name, rec in _load_section("servers").items():
        if not isinstance(name, str) or not isinstance(rec, dict):
            continue
        command = rec.get("command")
        if rec.get("reason") != REASON_COMMAND_NOT_FOUND or not isinstance(command, str):
            continue
        out[name[:_NAME_MAX_CHARS]] = {
            "command": command[:_COMMAND_MAX_CHARS],
            "reason": REASON_COMMAND_NOT_FOUND,
        }
    return out


def load_unused_own() -> dict[str, dict[str, str]]:
    """``{name: {"file", "command"}}`` from the latest rebuild; ``{}`` if none.

    Read as :func:`load_dropped` reads its half: an unusable store or record is
    left out, so the row carries no note.
    """
    out: dict[str, dict[str, str]] = {}
    for name, rec in _load_section("unused_own").items():
        if not isinstance(name, str) or not isinstance(rec, dict):
            continue
        file, command = rec.get("file"), rec.get("command")
        if not isinstance(file, str) or not isinstance(command, str):
            continue
        out[name[:_NAME_MAX_CHARS]] = {
            "file": file[:_COMMAND_MAX_CHARS],
            "command": command[:_COMMAND_MAX_CHARS],
        }
    return out
