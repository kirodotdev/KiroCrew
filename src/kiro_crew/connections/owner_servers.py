"""Which remote MCP servers the owner added, recorded where an agent cannot write.

The dashboard sign-in mints an approval URL for a remote server the owner
configured. The server's entry in the agent spec cannot be that proof on its
own: the spec is rebuilt from ``~/.kiro/crew/mcp.json`` and the shared Kiro
``mcp.json``, and a sandboxed process can append an entry to the first, or point
an existing name at a different URL. A sign-in offered on such an entry would
hand the owner a consent screen for a server an agent chose.

So the owner's add is recorded separately, in ``config.json`` under
``connections.owner_mcp_servers``: ``{name: {"url": url}}``. ``config.json`` is
sealed read-only for every sandboxed process, and the only writers of this key
are the owner-only dashboard routes that add, edit and remove a custom server.
A server is eligible for the dashboard sign-in only while its spec entry's URL
equals the URL recorded for its name. A server added any other way (by an
agent, by a session, by hand) has no record and keeps the chat guidance.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

#: ``config.json`` root shared with the operator OAuth client records.
CONFIG_ROOT_KEY = "connections"
#: The owner-added remote servers, keyed by exact server name.
CONFIG_SERVERS_KEY = "owner_mcp_servers"

#: Upper bound on recorded servers, so a corrupted record cannot grow unbounded.
MAX_RECORDS = 512
#: Upper bound on a recorded URL, in UTF-8 bytes. A longer URL is not recorded,
#: so that server keeps the chat guidance instead of growing ``config.json``.
MAX_URL_BYTES = 2048


def remote_url(entry: object) -> str | None:
    """The http(s) ``url`` of a remote MCP entry, or None for anything else.

    None as well for a URL that does not parse or is over :data:`MAX_URL_BYTES`.
    """
    if not isinstance(entry, Mapping):
        return None
    url = entry.get("url")
    if not isinstance(url, str) or len(url.encode("utf-8", "surrogatepass")) > MAX_URL_BYTES:
        return None
    try:
        scheme = urlsplit(url).scheme
    except ValueError:
        return None
    return url if scheme in ("http", "https") else None


def recorded_url(config: object, name: str) -> str | None:
    """The URL the owner recorded for ``name``, or None when there is no record."""
    if not isinstance(config, Mapping):
        return None
    root = config.get(CONFIG_ROOT_KEY)
    if not isinstance(root, Mapping):
        return None
    servers = root.get(CONFIG_SERVERS_KEY)
    if not isinstance(servers, Mapping):
        return None
    return remote_url(servers.get(name))


def is_signable_name(name: object) -> bool:
    """Whether ``name`` can carry an owner-added server's dashboard sign-in.

    A name that is also a registry slug is refused, so the registry path and
    this one never share a mint row under different URLs. A name that
    ``mcp_server_alias`` would rewrite (one with a ``/``, say) is refused,
    because the mint matches the engine's challenge by the spec key. The row
    flag and the mint both apply this, so the table never offers a sign-in the
    mint would refuse.
    """
    from kiro_crew.connections.registry import get_provider
    from kiro_crew.mcp_utils import mcp_server_alias

    if not isinstance(name, str) or not name:
        return False
    return mcp_server_alias(name) == name and get_provider(name.lower()) is None


def is_owner_server(config: object, name: str, entry: object) -> bool:
    """Whether ``entry`` is the remote server the owner recorded under ``name``."""
    url = remote_url(entry)
    return url is not None and recorded_url(config, name) == url


def _servers(cfg: dict[str, Any]) -> dict[str, Any]:
    root = cfg.get(CONFIG_ROOT_KEY)
    if not isinstance(root, dict):
        root = {}
        cfg[CONFIG_ROOT_KEY] = root
    servers = root.get(CONFIG_SERVERS_KEY)
    if not isinstance(servers, dict):
        servers = {}
        root[CONFIG_SERVERS_KEY] = servers
    return servers


def record(cfg: dict[str, Any], entries: Mapping[str, object]) -> dict[str, Any] | None:
    """``update_config_locked`` mutator: record the remote entries in ``entries``.

    A non-remote entry under a recorded name drops that name's record, so an
    owner edit that turns a server local leaves no stale grant behind. A server
    that cannot be recorded (an over-long URL, or the record already at
    :data:`MAX_RECORDS`) is logged and left out. Returns None, which writes
    nothing, when the record would not change.
    """
    servers = _servers(cfg)
    changed = False
    for name, entry in entries.items():
        url = remote_url(entry)
        if url is None:
            if isinstance(entry, Mapping) and isinstance(entry.get("url"), str):
                logger.warning("MCP server %r not recorded for sign-in: unusable url", name)
            changed = servers.pop(name, None) is not None or changed
            continue
        if remote_url(servers.get(name)) == url:
            continue
        if name not in servers and len(servers) >= MAX_RECORDS:
            logger.warning(
                "MCP server %r not recorded for sign-in: %d servers already recorded",
                name,
                MAX_RECORDS,
            )
            continue
        servers[name] = {"url": url}
        changed = True
    return cfg if changed else None


def forget(cfg: dict[str, Any], names: list[str]) -> dict[str, Any] | None:
    """``update_config_locked`` mutator: drop the records for ``names``."""
    root = cfg.get(CONFIG_ROOT_KEY)
    servers = root.get(CONFIG_SERVERS_KEY) if isinstance(root, dict) else None
    if not isinstance(servers, dict):
        return None
    changed = False
    for name in names:
        changed = servers.pop(name, None) is not None or changed
    return cfg if changed else None
