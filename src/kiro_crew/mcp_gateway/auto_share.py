"""Share every stdio MCP server by default, at gateway start.

With sharing on (``mcp_gateway.enabled``, the default), each declared stdio
server that nobody has decided about is stubbed: the gateway start records
``stub_overrides[name] = true`` in config, so the MCP Servers page shows the
real state and the person can turn the server off there. A person's off is
stored as ``false`` (``mcp_gateway_stubs._record_stub_decisions`` always stores
an off), and any stored decision, on or off, is never touched here, so an off
survives every restart. Turning sharing off skips this pass entirely.

The candidates are read from the same declaration fleet the rewriter wraps
(``folder_use.read_declarations``: every agent spec, the admitted crewmates and
the global settings file), and every decision is matched by the server's
canonical alias as well as its exact name, so an off saved under one spelling
holds for the other. The whole decision is taken again inside the config lock,
from the document as it stands then and the ``config.local.json`` overlay, so
a global off or a folder answer saved meanwhile wins.

Left per chat, with no step for the person:

* a server disabled in any declaration (never spawned: probing and running it
  are what consent gates);
* a never-pooled server (``rewriter_spec.is_unpoolable``);
* Kiro Crew's own ``kirocrew-*`` servers, which follow their own roster;
* a server one of whose declarations names the chat's folder in an arg or env
  value (``.``, a relative path, a folder placeholder:
  ``change_card_mcp_server.detects_chat_folder``), since a shared backend runs
  in one fixed folder;
* a server answered ``uses_cwd``, which the stub resolver already drops;
* every server, when one declaration source could not be read.

A server that asks a chat for its roots is kept per chat by the rewriter
(``folder_use.roots_keeps_per_chat``), whatever this pass recorded.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Collection, Mapping

logger = logging.getLogger(__name__)


def _aliases(names: Collection[str]) -> set[str]:
    from kiro_crew.mcp_utils import mcp_server_alias

    return {n for n in names if isinstance(n, str)} | {
        mcp_server_alias(n) for n in names if isinstance(n, str)
    }


def candidates(declarations: Any, gateway: Any) -> list[str]:
    """Names in *declarations* this pass would stub, given an ``McpGatewayConfig``. Pure.

    *declarations* is a ``folder_use.FolderDeclarations``; nothing is picked
    when it is incomplete.
    """
    from kiro_crew.mcp_gateway.rewriter_spec import is_unpoolable
    from kiro_crew.mcp_utils import mcp_server_alias

    if not gateway.enabled or not declarations.complete:
        return []
    decided = _aliases([*gateway.stub_overrides, *gateway.stub_servers, *gateway.cwd_use])
    kept = _aliases([*declarations.folder_named, *declarations.disabled])
    return sorted(
        name
        for name in declarations.stdio
        if not ({name, mcp_server_alias(name)} & (decided | kept)) and not is_unpoolable(name)
        # Kiro Crew's own servers follow their roster and leader
        # (``config.integration_sections.MANAGED_STUB_FOLLOWERS``); the opt-in
        # ones (``kirocrew-computer``, ``kirocrew-secrets``) stay a choice.
        and not mcp_server_alias(name).startswith("kirocrew-")
    )


def stale_on(declarations: Any, overrides: Mapping[str, Any]) -> list[str]:
    """Names *overrides* turns on that no read declaration carries any more. Pure.

    Their entry only makes the share list name a server that does not exist, so
    the locked pass drops it; a re-added server is shared again by default. An
    off is kept: it is the person's choice for that name. Nothing is picked
    when a declaration source could not be read.
    """
    if not declarations.complete:
        return []
    declared = _aliases(declarations.stdio)
    return sorted(
        name
        for name, on in overrides.items()
        if on is True and not ({name} | _aliases([name])) & declared
    )


def _declaration_paths() -> list[Any]:
    from kiro_crew.config.paths import kiro_agents_dir, mate_agents_dir

    agents = kiro_agents_dir()
    paths: list[Any] = [agents.parent / "settings" / "mcp.json"]
    for folder in (agents, mate_agents_dir()):
        try:
            paths.extend(sorted(p for p in folder.iterdir() if p.is_file()))
        except OSError:
            continue
    return paths


def declaration_files_stat() -> tuple[tuple[str, int, int], ...] | None:
    """``(path, mtime_ns, size)`` of every declaration file, as a cheap change gate. BLOCKING.

    ``None`` means there is no declaration source to watch, which ends the watch.
    """
    out: list[tuple[str, int, int]] = []
    for path in _declaration_paths():
        try:
            st = path.stat()
        except OSError:
            continue
        out.append((str(path), st.st_mtime_ns, st.st_size))
    return tuple(out)


def mcp_sources_fingerprint() -> tuple[tuple[str, bytes | None], ...]:
    """The MCP files the agent spec is built from, as they are on disk now. BLOCKING.

    ``~/.kiro/crew/mcp.json`` and kiro-cli's ``~/.kiro/settings/mcp.json``
    (``agent.user_mcp_files``): a hand edit there reaches the agent spec the
    rewriter wraps only through a rebuild.
    """
    import hashlib

    from kiro_crew.agent import user_mcp_files

    answer: list[tuple[str, bytes | None]] = []
    for path in user_mcp_files():
        try:
            digest: bytes | None = hashlib.sha256(path.read_bytes()).digest()
        except OSError:
            digest = None
        answer.append((str(path), digest))
    return tuple(answer)


def source_server_names() -> frozenset[str] | None:
    """Every server name the user's MCP files declare now, or ``None`` if one is unreadable. BLOCKING."""
    from kiro_crew import user_json
    from kiro_crew.agent import user_mcp_files

    names: set[str] = set()
    for path in user_mcp_files():
        if not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8")
            servers = user_json.loads_user_json(text).get("mcpServers", {})
        except (OSError, ValueError, AttributeError):
            return None
        if not isinstance(servers, dict):
            return None
        names.update(str(n) for n in servers)
    return frozenset(names)


def prune_removed_sources(
    previous: frozenset[str], pending: frozenset[str] = frozenset()
) -> tuple[frozenset[str], frozenset[str]]:
    """Delete from ``kirocrew.json`` the servers a user's MCP file stopped declaring. BLOCKING.

    The agent spec rebuild merges onto the existing ``kirocrew.json``, so a server
    deleted from ``mcp.json`` by hand would stay in the spec, and every chat would
    keep starting it. *previous* is what the files declared at the watch's last
    look, held by the watch in memory (nothing on disk an agent could edit), and
    *pending* the deletions an earlier look could not finish. Each name that left
    the files, and whose alias no declaration still carries, is removed the way
    an uninstall strips the rendered file, by its rendered alias. Returns
    ``(names declared now, deletions still pending)``: a spec that could not be
    read keeps its deletion pending for the next look. An unreadable MCP file
    removes nothing and keeps *previous*.
    """
    from kiro_crew import user_json
    from kiro_crew.config.paths import kiro_agents_dir
    from kiro_crew.dashboard.handlers.mcp import _get_mcp_lock_sync, _remove_from_agent_file
    from kiro_crew.mcp_utils import mcp_server_alias

    now = source_server_names()
    if now is None:
        return previous, pending  # A source could not be read: a missing name proves nothing.
    still_declared = {mcp_server_alias(n) for n in now}
    gone = {
        mcp_server_alias(n) for n in (previous - now) | pending if n not in now
    } - still_declared
    if not gone:
        return now, frozenset()
    spec = kiro_agents_dir() / "kirocrew.json"
    left: set[str] = set()
    removed: list[str] = []
    with _get_mcp_lock_sync():
        try:
            rendered = user_json.loads_user_json(spec.read_text(encoding="utf-8"))
            mounted = {mcp_server_alias(str(k)) for k in rendered.get("mcpServers", {})}
        except FileNotFoundError:
            mounted = set()
        except (OSError, ValueError, AttributeError):
            return now, frozenset(gone)  # Unreadable this moment: try again next look.
        for alias in sorted(gone):
            if alias not in mounted:
                continue
            if _remove_from_agent_file(spec, alias):
                removed.append(alias)
            else:
                left.add(alias)
    if removed:
        logger.info("mcp auto-share: removed from the agent spec: %s", ", ".join(removed))
    return now, frozenset(left)


def refresh_agent_specs() -> None:
    """Rebuild the agent specs from their MCP sources, as the gateway start does. BLOCKING."""
    from kiro_crew.mcp_gateway.launch_resolve import _refresh_agent_specs

    _refresh_agent_specs()


def declared_launches() -> frozenset[str] | None:
    """Digests of every declared stdio launch, or ``None`` when a source was unreadable. BLOCKING."""
    from kiro_crew.config.paths import kiro_agents_dir, mate_agents_dir
    from kiro_crew.mcp_gateway.folder_use import read_declarations

    facts = read_declarations(kiro_agents_dir(), mate_agents_dir())
    return frozenset(facts.launch_digest) if facts.complete else None


def _overlay_section() -> dict:
    """``mcp_gateway`` from ``config.local.json``, read as the loader reads it, or ``{}``."""
    from kiro_crew.config.loader import config_local_path, read_endorsed_config_text

    try:
        raw = json.loads(read_endorsed_config_text(config_local_path()))
    except (OSError, ValueError):
        return {}
    section = raw.get("mcp_gateway") if isinstance(raw, dict) else None
    return section if isinstance(section, dict) else {}


def share_declared_servers() -> list[str]:
    """Record a stub for every undecided stdio server and drop stale ones. BLOCKING.

    Returns the names whose entry changed (added or dropped), sorted.

    A failure leaves config as it was (those servers run per chat) and raises,
    so the caller can tell it from a pass with nothing to do and run it again.
    """
    from kiro_crew.config.loader import KiroCrewConfig, update_config_locked
    from kiro_crew.config.paths import kiro_agents_dir, mate_agents_dir
    from kiro_crew.config.resolution import _deep_merge
    from kiro_crew.config.section_builders import _build_mcp_gateway_config
    from kiro_crew.mcp_gateway.folder_use import read_declarations

    added: list[str] = []
    # A cheap first read, so a start with nothing to share takes no lock.
    if not KiroCrewConfig.load().mcp_gateway.enabled:
        return []
    declarations = read_declarations(kiro_agents_dir(), mate_agents_dir())
    overlay = _overlay_section()
    # Steady state (every server decided, none gone) takes no config lock
    # and writes nothing; the locked pass below decides again before it writes.
    current = KiroCrewConfig.load().mcp_gateway
    if not candidates(declarations, current) and not stale_on(declarations, current.stub_overrides):
        return []

    def _mutate(data: dict) -> dict | None:
        section = data.setdefault("mcp_gateway", {})
        if not isinstance(section, dict):
            return None
        # Decided again on the locked document: a global off, a decision or a
        # folder answer saved since the first read wins.
        gateway = _build_mcp_gateway_config(_deep_merge(section, overlay))
        overrides = section.get("stub_overrides")
        overrides = dict(overrides) if isinstance(overrides, dict) else {}
        fresh = candidates(declarations, gateway)
        gone = stale_on(declarations, overrides)
        if not fresh and not gone:
            return None
        overrides.update({name: True for name in fresh})
        for name in gone:
            overrides.pop(name, None)
        if overrides:
            section["stub_overrides"] = {n: overrides[n] for n in sorted(overrides)}
        else:
            section.pop("stub_overrides", None)
        added.extend(sorted({*fresh, *gone}))
        return data

    update_config_locked(mutate=_mutate)
    if added:
        logger.info(
            "mcp auto-share: sharing %s (turn one off on the MCP Servers page)",
            ", ".join(added),
        )
    return added
