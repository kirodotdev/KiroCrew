"""The ``mcp.json`` servers an agent spec's opt-in mounts beside its own ``mcpServers``.

A spec can ask its host to load the user's MCP config files as well as the servers
it defines, with ``includeMcpJson``. kiro-cli then also starts the enabled servers of
the user-level ``mcp.json`` and of the session checkout's ``.kiro/settings/mcp.json``
that the spec does not define itself, and reports each one's start as it does a
spec server's. KAS is handed the flag on the agent definition Crew projects for it
and shows those servers' tools.

Crew's own readers of "the servers this agent has" (the unattended-run MCP wait,
the unresolved-``@server``-ref guard and the dashboard's per-agent server list) read
the spec's ``mcpServers``. This module adds what the opt-in mounts, so a cron on such
an agent waits for and names those servers too, and a ``tools`` ref to one of them
is not reported as naming nothing.

It reads nothing of its own. The opt-in is
:func:`kiro_crew.acp.kas_settings_mcp.include_mcp_json`, the flag the KAS projection
sends, and the files are :func:`kiro_crew.acp.session_mcp.native_settings_sources`,
the credential-gated read a session's restrictions come from, so the two answers
cannot drift apart. Both are imported per call: ``kiro_crew.acp`` imports this
package at load time.

:func:`mcp_json_server_names` reads files, so a session path calls it in the
off-loop hop that already reads the agent spec, never at the guard. Nothing here
raises: an unreadable, refused or malformed file contributes no servers.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS, ACP_BACKEND_KIRO
from kiro_crew.agent_sdk.mcp_refs import parse_tools_refs
from kiro_crew.mcp_cleanup import mcp_entries_muted

logger = logging.getLogger(__name__)

#: The hosts that load ``mcp.json`` through the spec; any other loads none.
_MCP_JSON_HOSTS = frozenset({ACP_BACKEND_KIRO, ACP_BACKEND_KAS})

#: The answer for a spec that states no bool under either key. KAS reads a missing
#: flag as false (except for the legacy agent files ``acp.kas_mcp_json_legacy``
#: keeps on the old reading, which this does not model). What kiro-cli does with
#: one is not settled, and Crew's own readers disagree about it, so that decision
#: is made here, in one place. Until then only a stated opt-in counts. A true
#: default would make every unattended run on an agent without the key wait for
#: the whole user-level file.
UNSTATED_OPT_IN = False

#: Bounds on the names one read returns. The answer is kept for a session's life
#: (``AcpClient._mcp_json_names``, the runtime's guard hand-off) and read on the
#: event loop by the cron wait and the ref guard, and both files can be written by
#: the checkout or the agent, so neither the count nor a name's length is left to
#: them. A longer name is left out whole, not cut, because a cut name would name a
#: different server. A server the spec's ``tools`` refs by name is kept past both
#: bounds, so a ref is never reported as naming nothing because of them; those
#: names are already held in the spec snapshot. 256 of each is wider than any
#: hand-written ``mcp.json``, as ``MAX_HARNESS_CONFIG_MCP_SERVERS`` is for a
#: harness config.
MAX_MCP_JSON_NAMES = 256
MAX_MCP_JSON_NAME_LEN = 256


def includes_mcp_json(spec: Any, backend: str) -> bool:
    """Whether *spec* asks *backend* to mount the servers of the user's ``mcp.json``.

    A non-bool value states nothing, so the next key, then :data:`UNSTATED_OPT_IN`,
    decides. False for a spec that is not an object and for a host that reads no
    ``mcp.json`` through the spec.
    """
    if backend not in _MCP_JSON_HOSTS or not isinstance(spec, Mapping):
        return False
    from kiro_crew.acp.kas_settings_mcp import include_mcp_json

    stated = include_mcp_json(spec)
    return UNSTATED_OPT_IN if stated is None else stated


def _settings_servers(work_dir: str | Path | None) -> list[Mapping[str, Any]]:
    # The files a session's restrictions are read from, global first. One that
    # cannot be read drops the checkout's file, as the KAS projection does
    # (``kas_settings_mcp._file_servers``); a global one Crew cannot read leaves none.
    from kiro_crew.acp.session_mcp import NativeSettingsUnreadable, native_settings_sources

    for scope in (work_dir, None) if work_dir else (None,):
        try:
            return [source.servers for source in native_settings_sources(scope)]
        except NativeSettingsUnreadable:
            logger.debug("mcp.json reach: settings unreadable", exc_info=True)
        except Exception:
            logger.debug("mcp.json reach: settings read failed", exc_info=True)
            return []
    return []


def mcp_json_server_names(
    spec: Any, backend: str, work_dir: str | Path | None = None
) -> tuple[str, ...]:
    """The enabled ``mcp.json`` servers *spec*'s opt-in mounts on *backend*.

    Empty unless :func:`includes_mcp_json`. Otherwise the servers of the user-level
    ``mcp.json`` and, when *work_dir* is given, of
    ``<work_dir>/.kiro/settings/mcp.json``, in that order, leaving out:

    * a name the spec defines itself, whose own entry is the one the host mounts;
    * an entry that is not an object, which defines no server;
    * a server muted in either file, by the launch's own rule
      (:func:`~kiro_crew.mcp_cleanup.mcp_entries_muted`);
    * a name longer than :data:`MAX_MCP_JSON_NAME_LEN`, and every name past the
      first :data:`MAX_MCP_JSON_NAMES`, unless the spec's ``tools`` refs it.

    Blocking: run it off the event loop.
    """
    if not includes_mcp_json(spec, backend):
        return ()
    own = spec.get("mcpServers")
    defined = set(own) if isinstance(own, Mapping) else set()
    entries: dict[str, list[Mapping[Any, Any]]] = {}
    for servers in _settings_servers(work_dir):
        for name, entry in servers.items():
            if isinstance(name, str) and name and name not in defined:
                if isinstance(entry, Mapping):
                    entries.setdefault(name, []).append(entry)
    _grant_all, refs = parse_tools_refs(spec.get("tools"))
    referenced = set(refs)
    names: list[str] = []
    omitted = 0
    for name, found in entries.items():
        if mcp_entries_muted(found):
            continue
        fits = len(name) <= MAX_MCP_JSON_NAME_LEN and len(names) < MAX_MCP_JSON_NAMES
        if fits or name in referenced:
            names.append(name)
        else:
            omitted += 1
    if omitted:
        logger.warning("mcp.json reach: %d enabled server(s) left out by the name bounds", omitted)
    return tuple(names)
