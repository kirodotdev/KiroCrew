"""Keep an MCP server's ``env`` on this machine when its spec is projected onto KAS.

On KAS an agent's MCP servers travel to the engine in ``session/new``
(``_meta.kiro.customAgents``), and the engine stores that batch verbatim in the
session's ``session.json``. So :func:`kiro_crew.acp.kas_agents._withhold_credential_fields`
keeps env values off the wire. A stdio server that needs one of them would start
without it: it fails to start and is missing from the tool list, or runs without
its setting. On kiro-cli v3 the engine reads the agent file from disk, so the env
never enters the session record there.

This module gives the server its values without putting one on the wire. For one stdio server
of one session it writes a LAUNCH SNAPSHOT -- the server's command, args, the
env values the wire withheld and the gateway's Kerberos and ssh-agent pointers a
kiro-cli child would inherit (:func:`inherited_auth_env`), nothing else of the
agent -- into
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

A stdio launch and a header bridge are SHARED: KAS runs one MCP pool per
process and restarts a server whose declaration changed, so the reference names
no session (:data:`SHARED_SESSION`) and its version is an HMAC of the file's
content under a per-process key (:func:`_shared_version`). Every session of the
same agent then sends the same declaration and reads the same file; the gateway
counts the projections holding it (:data:`_SHARED_HOLDS`) and
:func:`discard_batch` removes it with the last. A Crew-owned Connection bridge
keeps a random version per write, so each projection writes a file no other
shares, and its holder removes exactly the files it references when it shuts
down (:func:`discard_batch`). A snapshot
nobody removed (a provider that never shut down, a batch a reload replaced) stays
until the next gateway boot, whose background sweep removes those last written
:data:`_STALE_AFTER_SECS` before that boot (:func:`sweep_stale`). Blocking file I/O throughout;
callers run it off the event loop.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
from collections import Counter
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write
from kiro_crew.kas_env_shim import (
    BRIDGE_LEAF,
    SHIM_MODULE,
    SNAPSHOT_FORMAT,
    SNAPSHOT_LEAF,
    SNAPSHOT_MARKER,
    snapshot_name,
)
from kiro_crew.kas_http_bridge import _LOOPBACK_HOSTS, _normalized_host

logger = logging.getLogger(__name__)

#: The boot sweep removes a snapshot last written this long before boot. The
#: previous run's agent processes are already ended by then, so the age only
#: protects a second gateway that shares this data home and still runs sessions.
_STALE_AFTER_SECS = 7 * 24 * 3600


def snapshot_dir(leaf: str = SNAPSHOT_LEAF) -> Path:
    """The owner-only directory snapshots live in, under the crew data home.

    Connection bridge snapshots use :data:`BRIDGE_LEAF`, which an enforced harness's
    child may read; every other snapshot (env values) stays in the withheld leaf.
    """
    from kiro_crew.config.paths import config_dir

    return config_dir() / leaf


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


def inherited_auth_env(forward_ssh_auth_sock: bool) -> dict[str, str]:
    """The gateway's own ``KRB5CCNAME`` and ``SSH_AUTH_SOCK``, for a server KAS starts.

    kiro-cli's MCP children inherit its environment, so they see the two pointers
    the gateway armed for it. KAS starts a stdio server with a fixed minimal
    environment instead, so the launcher hands the server the same two, read from
    the gateway's live environment under the rules the KAS spawn applies:

    * ``KRB5CCNAME`` as :func:`kiro_crew.env.resolve_krb5_ccname` settles it. On
      Linux an unset or keyring value becomes the user's own ``FILE:`` cache when
      one exists, because the sandbox's user namespace reads the keyring default
      as empty.
    * ``SSH_AUTH_SOCK`` only when *forward_ssh_auth_sock*: the verdict the launch
      tail applied to the running engine's own environment (``acp.launch``, as
      :func:`kiro_crew.sandbox.wrap_argv` re-judged it), never one re-derived
      now, after a config or governance change. A server never gets a socket
      its engine was denied.

    Both name where a credential lives; neither is one, and the cache file and
    the socket stay behind the same sandbox. Blocking: it stats the cache.
    """
    # Call-time, like the config loader below: this leaf keeps the env resolver
    # off its import graph.
    from kiro_crew.env import resolve_krb5_ccname

    out: dict[str, str] = {}
    krb5 = {"KRB5CCNAME": os.environ["KRB5CCNAME"]} if "KRB5CCNAME" in os.environ else {}
    resolve_krb5_ccname(krb5)
    if krb5.get("KRB5CCNAME"):
        out["KRB5CCNAME"] = krb5["KRB5CCNAME"]
    sock = os.environ.get("SSH_AUTH_SOCK", "")
    if sock and forward_ssh_auth_sock:
        out["SSH_AUTH_SOCK"] = sock
    return out


def launch_through_shim(
    projected: dict[str, Any],
    *,
    session_key: str,
    agent_id: str,
    server: str,
    env: Mapping[str, str],
    inherited: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """*projected* rewritten to start through the launcher, its snapshot written.

    *env* is what the wire withheld (:func:`withheld_env`); placeholders in it are
    expanded here, against the same filtered view the gateway's own sidecar uses,
    because the launcher runs with the engine's reduced environment. *inherited*
    (:func:`inherited_auth_env`) is written under it as given: those are the
    gateway's own values, not text to expand, and a key *env* names wins. Raises
    ``OSError`` when the snapshot cannot be written.
    """
    from kiro_crew.mcp_gateway.rewriter_env import _expand_env_map

    args = projected.get("args")
    launch: dict[str, Any] = {
        "command": projected["command"],
        "args": [a for a in args if isinstance(a, str)] if isinstance(args, list) else [],
        "env": {**(inherited or {}), **_expand_env_map(dict(env), server=server)},
    }
    version = _write_snapshot(
        launch, session_key=SHARED_SESSION, agent_id=agent_id, server=server, shared=True
    )
    return _shim_entry(projected, SHARED_SESSION, agent_id, server, version)


#: The session part of a shared launch reference. KAS keeps one MCP pool
#: per process, merges every session's declarations by server name and restarts a
#: server whose declaration changed, so a per-session reference would make each
#: new session on a shared runtime kill the server for all of them. A stdio launch or a
#: header bridge holds only what the agent file says, the same for every session of
#: that agent, so its reference names no session.
SHARED_SESSION = "shared"
#: The key a shared reference's version is an HMAC under. Per process, in memory
#: only, never written or logged: the engine stores the reference in
#: ``session.json``, and a plain digest of the launch would let anyone holding that
#: file test guesses at the env values. A new process keys new versions, so two
#: gateways on one data home never share a file.
_SHARE_KEY = secrets.token_bytes(32)
#: How many recorded projections hold each shared snapshot, by path. Written and
#: released under :data:`_SHARE_LOCK`, which also covers the file write and the
#: removal, so a release can never delete a file a concurrent projection rewrote.
_SHARED_HOLDS: Counter[str] = Counter()
_SHARE_LOCK = threading.Lock()


def _shared_version(launch: Mapping[str, Any], leaf: str, agent_id: str, server: str) -> str:
    """The version of a shared snapshot: an HMAC over everything the file holds."""
    body = json.dumps(
        {"leaf": leaf, "agent": agent_id, "server": server, "launch": launch}, sort_keys=True
    )
    return hmac.new(_SHARE_KEY, body.encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def _write_snapshot(
    launch: Mapping[str, Any],
    *,
    session_key: str,
    agent_id: str,
    server: str,
    leaf: str = SNAPSHOT_LEAF,
    shared: bool = False,
) -> str:
    """Write one owner-only snapshot holding *launch* and its reference; its version.

    A *shared* snapshot's version is :func:`_shared_version`, so every projection of
    the same launch writes the same file; each write takes one hold on it, and
    :func:`discard_batch` removes the file when the last hold is released. Any
    other version is random per write.
    """
    directory = snapshot_dir(leaf)
    platform_compat.make_owner_only_dir(directory)
    if shared:
        version = _shared_version(launch, leaf, agent_id, server)
    else:
        version = secrets.token_hex(16)
    name = snapshot_name(session_key, agent_id, server, version)
    doc = {
        **launch,
        SNAPSHOT_MARKER: SNAPSHOT_FORMAT,
        "session": session_key,
        "agent": agent_id,
        "server": server,
        "version": version,
    }
    with _SHARE_LOCK if shared else contextlib.nullcontext():
        # Owner-only before any byte is written, and it raises if that lockdown fails.
        atomic_write(
            directory / name,
            json.dumps(doc, sort_keys=True).encode("utf-8"),
            fsync=True,
            restrict_to_owner=True,
        )
        if shared:
            _SHARED_HOLDS[str(directory / name)] += 1
    return version


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
    forward_ssh_auth_sock: bool = False,
) -> tuple[dict[str, Any], bool, bool]:
    """``(entry, launched, complete)``: *projected* through the launcher when the wire
    withheld env values it needs or the gateway holds auth pointers for it
    (:func:`inherited_auth_env`, given *forward_ssh_auth_sock*), else *projected*
    unchanged.

    A pointer goes under the server's own env: a key the spec sets, or one the
    wire kept (a relayed ``${NAME}``), wins. *complete* says every withheld value
    went into the snapshot, so the caller's "not relaying env" log line has
    nothing left to name. When the snapshot cannot be written the entry still
    points at the launcher, with a reference no snapshot answers, so the server
    refuses to start rather than run without its env. A server that only had
    pointers pending is handed back as projected, so it starts as it would
    without them.
    """
    if not session_key or not launchable(projected):
        return projected, False, False
    env = withheld_env(original_env, projected.get("env"))
    kept = projected.get("env") or {}
    inherited = {
        k: v for k, v in inherited_auth_env(forward_ssh_auth_sock).items() if k not in kept
    }
    if not env and not inherited:
        return projected, False, False
    declared = original_env if isinstance(original_env, dict) else {}
    every = {k for k in declared if isinstance(k, str) and k and k not in kept}
    try:
        entry = launch_through_shim(
            projected,
            session_key=session_key,
            agent_id=agent_id,
            server=server,
            env=env,
            inherited=inherited,
        )
    except OSError:
        if not env:
            logger.warning(
                "agent %r: could not write the local launch for MCP server %r; it "
                "starts without the gateway's Kerberos and ssh-agent pointers",
                agent_id,
                server,
                exc_info=True,
            )
            return projected, False, False
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
    # A malformed (non-dict) env relays nothing, so a launch the pointers alone
    # caused must not clear its "withheld" report.
    return entry, True, isinstance(original_env, dict) and every <= set(env)


def kas_accepts_url(url: Any) -> bool:
    """Whether KAS connects to *url*: ``https``, or ``http`` to a loopback host.

    The same test as KAS's ``isHttpsOrLocalhost`` (``mcp/url-validation.ts``),
    which refuses any other url as ``insecure-url``.
    """
    if not isinstance(url, str):
        return False
    try:
        parts = urlsplit(url.strip())
        host = parts.hostname
        parts.port  # noqa: B018 -- raises on a port KAS's URL parser also refuses
    except ValueError:
        return False
    scheme = parts.scheme.lower()
    return scheme == "https" or (scheme == "http" and _normalized_host(host) in _LOOPBACK_HOSTS)


def bridge_url(url: str) -> str:
    """*url* as the bridge must dial it: a plain ``http`` loopback host in its
    canonical spelling (``127.0.0.1``, ``[::1]``, ``localhost``).

    The loopback test normalises spellings such as ``127.0.0.1.``, which the OS
    resolver does not read as numeric and would look up through DNS. Dialling
    the canonical host keeps the headers on this machine. Port, path and query
    are kept; any other url is returned stripped, as :func:`bridgeable` read it.
    """
    url = url.strip()
    parts = urlsplit(url)
    if parts.scheme.lower() != "http":
        return url
    host = _normalized_host(parts.hostname)
    if host not in _LOOPBACK_HOSTS:
        return url
    # Userinfo is dropped: urllib would hand ``user@host`` to the resolver as the
    # host name, and it never sends userinfo as credentials anyway.
    netloc = f"[{host}]" if ":" in host else host
    if parts.port is not None:
        netloc = f"{netloc}:{parts.port}"
    return urlunsplit(("http", netloc, parts.path, parts.query, parts.fragment))


def refuses_plain_http_headers() -> bool:
    """Whether ``agent.kas_refuse_plain_http_mcp_headers`` is a JSON ``true``.

    Off by default, and off when the config cannot be loaded, so a header-bearing
    ``http://`` server on another host keeps connecting through the bridge.
    """
    try:
        # Call-time: the config loader pulls in the config plane this leaf stays off.
        from kiro_crew.config.loader import KiroCrewConfig

        return KiroCrewConfig.load().agent.kas_refuse_plain_http_mcp_headers is True
    except Exception:
        logger.debug("config unavailable; relaying plain-http MCP headers", exc_info=True)
        return False


def bridgeable(entry: Mapping[str, Any]) -> bool:
    """Whether a projected entry is a remote server the launcher can bridge to.

    Streamable HTTP only: an ``sse`` entry speaks the older two-endpoint transport,
    which the bridge (:mod:`kiro_crew.kas_http_bridge`) does not, so its headers
    stay withheld. A url KAS itself would accept (:func:`kas_accepts_url`) is
    bridged. So is plain ``http`` to another host, which KAS refuses but reaches
    as the bridge's local stdio server, unless :func:`refuses_plain_http_headers`:
    that bridge sends the headers in cleartext.
    """
    url = entry.get("url")
    if not isinstance(url, str) or entry.get("command") or entry.get("type") == "sse":
        return False
    url = url.strip()  # as kas_accepts_url and bridge_url read it
    if not url.lower().startswith(("https://", "http://")):
        return False
    if kas_accepts_url(url):
        return True
    try:
        urlsplit(url.strip()).port  # noqa: B018 -- a url that does not parse is not dialled
    except ValueError:
        return False
    return not refuses_plain_http_headers()


def relay_withheld_headers(
    projected: dict[str, Any],
    original_headers: Any,
    *,
    session_key: str,
    agent_id: str,
    server: str,
) -> tuple[dict[str, Any], bool]:
    """``(entry, relayed)``: a remote *projected* entry with withheld headers,
    rewritten to start the launcher as a local stdio bridge, else unchanged.

    The snapshot holds the url and the header values; the engine gets the same
    launcher reference a stdio server does, so the values never cross the wire.
    *relayed* says the headers went into the snapshot. When it cannot be written
    the entry still points at the launcher with a reference no snapshot answers,
    so the server reports why it did not start instead of connecting without its
    headers and falling into a sign-in it never needs.
    """
    if not session_key or not isinstance(original_headers, dict) or not bridgeable(projected):
        return projected, False
    headers = {
        k: str(v)
        for k, v in original_headers.items()
        if isinstance(k, str) and k and isinstance(v, (str, int, float))
    }
    if not headers:
        return projected, False
    from kiro_crew.mcp_gateway.rewriter_env import _expand_env_map

    stdio = {k: v for k, v in projected.items() if k not in ("url", "type", "headers", "env")}
    try:
        version = _write_snapshot(
            {
                "url": bridge_url(projected["url"]),
                "headers": dict(_expand_env_map(headers, server=server)),
            },
            session_key=SHARED_SESSION,
            agent_id=agent_id,
            server=server,
            shared=True,
        )
    except OSError:
        logger.warning(
            "agent %r: could not write the local bridge for MCP server %r; it will "
            "refuse to start rather than connect without its headers",
            agent_id,
            server,
            exc_info=True,
        )
        return _shim_entry(stdio, session_key, agent_id, server, _UNWRITTEN_VERSION), False
    return _shim_entry(stdio, SHARED_SESSION, agent_id, server, version), True


def crew_connection_slug(name: str, entry: Mapping[str, Any], backend: str) -> str:
    """The provider slug when Crew owns this server's OAuth, else ``""``.

    Only a registry provider's own entry (slug AND url match), for a session on
    *backend* while :func:`kiro_crew.connections.crew_oauth.custody_on` holds and
    the backend is bridged. Blocking: reads config.
    """
    from kiro_crew.acp.kas_app_identity import gateway_bound_port
    from kiro_crew.connections import crew_oauth
    from kiro_crew.connections.oauth_clients import provider_for_server

    # Only the gateway serves the proxy: a session projected elsewhere
    # (``kirocrew chat``) keeps the entry's native path.
    if not gateway_bound_port():
        return ""
    return crew_oauth.routed_slug(name, entry, provider_for_server, backend=backend)


def crew_entry(
    projected: dict[str, Any],
    name: str,
    entry: Mapping[str, Any],
    session_key: str,
    agent_id: str,
    backend: str,
) -> dict[str, Any] | None:
    """*projected* rewritten as a Crew-owned Connection bridge, or ``None`` if not one."""
    from kiro_crew.acp.kas_app_identity import gateway_bound_port

    slug = crew_connection_slug(name, entry, backend)
    if not slug:
        return None
    return relay_crew_connection(
        projected,
        session_key=session_key,
        agent_id=agent_id,
        server=name,
        slug=slug,
        port=gateway_bound_port(),
        headers=entry.get("headers"),
    )


def bridge_array(
    elements: list[Any], *, backend: str, session_key: str, agent_id: str
) -> tuple[list[Any], list[str]]:
    """``(elements, written)``: an ACP ``mcpServers`` array with each Crew-owned
    Connection replaced by the shim stdio element, and the snapshot names written.

    The array-channel twin of the KAS projection's :func:`crew_entry`. The holder
    hands *written* to :func:`discard_batch` when its process ends. Blocking.
    """
    if not session_key:
        return elements, []
    from kiro_crew.acp.session_mcp import acp_server_element

    out: list[Any] = []
    written: list[str] = []
    for element in elements:
        shim = None
        if isinstance(element, dict) and element.get("type") in ("http", "sse"):
            pairs = element.get("headers") or []
            headers = {
                p["name"]: p["value"]
                for p in pairs
                if isinstance(p, dict) and isinstance(p.get("name"), str)
            }
            url = element.get("url")
            entry = {"url": url, "headers": headers, "type": element.get("type")}
            name = str(element.get("name") or "")
            crew = crew_entry({"url": url}, name, entry, session_key, agent_id, backend)
            shim = acp_server_element(name, crew) if crew is not None else None
        if shim is None:
            out.append(element)
            continue
        out.append(shim)
        written += batch_snapshot_names([{"mcpServers": {shim["name"]: crew}}])
    return out, written


def connection_asks(
    permissions: Any, mcp_servers: Mapping[str, Any], session_key: str
) -> dict[str, Any] | None:
    """:func:`with_connection_asks` over this session's bridged servers; ``None`` if none."""
    if not session_key or not mcp_servers:
        return None
    from kiro_crew.connections import crew_oauth_proxy

    servers = [n for n in mcp_servers if crew_oauth_proxy.is_connection_server(session_key, n)]
    return with_connection_asks(permissions, servers) if servers else None


def with_connection_asks(permissions: Any, servers: list[str]) -> dict[str, Any]:
    """*permissions* plus one ``ask`` covering every tool of the Connection *servers*.

    Every call of a Crew-owned Connection must raise a permission request: the
    gateway proxy forwards a ``tools/call`` only against the approval Crew's gate
    grants on it. KAS resolves the most restrictive rule, so this ``ask`` outranks
    any derived allow.
    """
    block = dict(permissions) if isinstance(permissions, dict) else {}
    match = [f"{n}/*" for n in sorted(servers)]
    block["rules"] = [
        *(block.get("rules") or []),
        {"capability": "mcp", "match": match, "effect": "ask"},
    ]
    return block


def relay_crew_connection(
    projected: dict[str, Any],
    *,
    session_key: str,
    agent_id: str,
    server: str,
    slug: str,
    port: str,
    headers: Any = None,
) -> dict[str, Any]:
    """*projected* (a Crew-owned Connection) rewritten to start the gateway bridge.

    The snapshot names the connection, the gateway port and a per-bridge secret;
    no url, header or token. The entry's own non-credential *headers* (a
    ``User-Agent``) are kept in the gateway and sent beside Crew's bearer. The gateway registers the same secret, so only the
    launcher holding this snapshot is admitted by its proxy
    (:mod:`kiro_crew.connections.crew_oauth_proxy`). When the snapshot cannot be
    written the entry still points at the launcher with a reference no snapshot
    answers, so the server refuses to start rather than fall back to the vendor.
    """
    from kiro_crew.connections import crew_oauth_proxy

    stdio = {
        k: v for k, v in projected.items() if k not in ("url", "type", "headers", "env", "oauth")
    }
    mcp_url = str(projected.get("url") or "")
    token = crew_oauth_proxy.new_bridge_token()
    try:
        version = _write_snapshot(
            {"connection": slug, "port": int(port), "bridge_token": token},
            session_key=session_key,
            agent_id=agent_id,
            server=server,
            leaf=BRIDGE_LEAF,
        )
    except (OSError, ValueError):
        logger.warning(
            "agent %r: could not write the Connection bridge for MCP server %r; it will "
            "refuse to start",
            agent_id,
            server,
            exc_info=True,
        )
        return _shim_entry(stdio, session_key, agent_id, server, _UNWRITTEN_VERSION)
    name = snapshot_name(session_key, agent_id, server, version)
    if not crew_oauth_proxy.register_bridge(
        version=version,
        token=token,
        session_key=session_key,
        server=server,
        slug=slug,
        mcp_url=mcp_url,
        snapshot=name,
        headers=headers if isinstance(headers, Mapping) else None,
    ):
        logger.warning(
            "agent %r: the Connection bridge table is full; MCP server %r will refuse "
            "to start in this session",
            agent_id,
            server,
        )
        _remove([snapshot_dir(BRIDGE_LEAF) / name])
        return _shim_entry(stdio, session_key, agent_id, server, _UNWRITTEN_VERSION)
    return _shim_entry(stdio, session_key, agent_id, server, version)


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
    cutoff = boot_time - _STALE_AFTER_SECS
    for leaf in (SNAPSHOT_LEAF, BRIDGE_LEAF):
        try:
            stale: list[Path] = []
            for path in snapshot_dir(leaf).iterdir():
                try:
                    if path.stat().st_mtime < cutoff:
                        stale.append(path)
                except FileNotFoundError:
                    continue
            _remove(stale)
        except FileNotFoundError:
            continue
        except OSError:
            # Pruning is housekeeping; a projection never fails for it.
            logger.warning("could not prune KAS launch snapshots", exc_info=True)


def _flag(args: list[Any], flag: str) -> str:
    try:
        value = args[args.index(flag) + 1]
    except (ValueError, IndexError):
        return ""
    return value if isinstance(value, str) else ""


def is_crew_bridge_launch(element: Any) -> bool:
    """Whether *element* is exactly the launch :func:`relay_crew_connection` wrote.

    The executable and the whole argv must be the launcher's, and its reference
    must name a registered bridge of this session and server: a server whose args
    merely mention the launcher is not one. Read before handing a bridge the
    agent-socket pickup (``acp.ipc_pickup``).
    """
    from kiro_crew.connections import crew_oauth_proxy

    if not isinstance(element, dict):
        return False
    args = element.get("args")
    if not isinstance(args, list) or len(args) < 8:
        return False
    # The reference values sit in the last four flag slots; the launch is rebuilt
    # from them the way the projection built it, and must match exactly.
    session, agent, server, version = (str(v) for v in args[-7::2])
    expected = _shim_entry({}, session, agent, server, version)
    if (element.get("command"), args) != (expected["command"], expected["args"]):
        return False
    return server == element.get("name") and crew_oauth_proxy.bridge_matches(
        version, session, server
    )


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

    A shared snapshot (:data:`SHARED_SESSION`) is released, not removed, and goes
    only with its last hold: once per recorded write in *written*, or once for a
    name only the batch carries. shortcut: a projection nobody records (a failed
    create) keeps its hold, so that file waits for the boot sweep, as an
    unrecorded per-session file always has.
    """
    try:
        batch = list(batch_snapshot_names(custom_agents))
        recorded = [n for n in written if isinstance(n, str)] if isinstance(written, list) else []
        names = [*batch, *recorded]
        # Each Connection bridge lives exactly as long as its snapshot.
        from kiro_crew.connections import crew_oauth_proxy

        crew_oauth_proxy.unregister_snapshots(names)
        releases = Counter(recorded)
        for name in batch:
            releases[name] = max(releases[name], 1)
        with _SHARE_LOCK:
            gone: list[Path] = []
            for name, count in releases.items():
                if Path(name).name != name:
                    continue
                for leaf in (SNAPSHOT_LEAF, BRIDGE_LEAF):
                    path = snapshot_dir(leaf) / name
                    key = str(path)
                    if key in _SHARED_HOLDS:
                        _SHARED_HOLDS[key] -= count
                        if _SHARED_HOLDS[key] > 0:
                            continue
                        del _SHARED_HOLDS[key]
                    gone.append(path)
            # Inside the lock: a projection rewriting this file waits for the removal.
            _remove(gone)
    except OSError:
        logger.warning("could not remove KAS launch snapshots", exc_info=True)
