"""Reading and editing one server's entry in each config scope.

Finding a server's spec in any scope, and setting, replacing or removing its entry in the
Kiro Crew file, a provider global file or an agent file, plus its per-tool overrides.

Part of the ``kiro_crew.dashboard.handlers.mcp`` family: callers and tests reach every
name here as ``kiro_crew.dashboard.handlers.mcp.<name>``, and a patch there reaches this
module too (:mod:`kiro_crew._facade_mirror`). This module never imports the facade.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from kiro_crew.agent_discovery import _read_agent_spec
from kiro_crew.config.paths import kiro_agents_dir
from kiro_crew.dashboard.handlers.mcp_config_files import (
    _GLOBAL_MCP_JSON,
    AmbiguousServerKey,
    _atomic_write,
    _config_entry_for,
    _config_key_for,
    _extra_mcp_scopes,
    _kirocrew_mcp_json,
    _load_json_or_empty,
    _resolve_key_from_read,
)
from kiro_crew.mcp_cleanup import mcp_entry_is_muted


def _find_server_spec_anywhere(name: str) -> dict | None:
    """Locate a server's full spec from any known source.

    Search order matches the Kiro Crew merge: agent config → <data home>/mcp.json
    (``config_dir()/mcp.json``, i.e. ~/.kiro/crew/mcp.json) → kiro global →
    edition-contributed provider scopes.  Returns a shallow copy with
    ``disabled`` stripped (the caller decides whether to disable in its target
    scope).

    The agents-dir candidate is an AGENT SPEC and is read through the hardened
    reader (size cap, sensitive-symlink screen, non-object rejection, SEL denial
    event) rather than the plain loader. It is statically first in merge order,
    so it is read before the loop instead of being tagged inside one -- a
    refused spec then degrades exactly as ``_load_json_or_empty``'s ``{}`` did,
    contributing nothing and falling through to the next scope. The remaining
    candidates are provider ``mcp.json`` files, not agent specs, so the
    agent-spec reader does not describe them.
    """

    def _usable(data: dict[str, Any]) -> dict | None:
        # By raw key or alias: a slash-named entry serves its canonical row.
        spec = _config_entry_for(data.get("mcpServers", {}), name)
        if isinstance(spec, dict) and (spec.get("command") or spec.get("url")):
            return {k: v for k, v in spec.items() if k != "disabled"}
        return None

    found = _usable(
        _read_agent_spec(
            kiro_agents_dir() / "kirocrew.json",
            operation="mcp_find_server_spec",
            source="dashboard",
        )
        or {}
    )
    if found is not None:
        # The rendered spec is a PROJECTION: for a pre-registered Connections
        # provider it carries the operator's OAuth client (secret included),
        # bound there at write time from the vault. A copy taken from it for a
        # source scope would be a second, unmanaged home for that secret --
        # one the client's removal or rotation never reaches. Strip the three
        # owned keys so the scope copy holds only what the sources hold; the
        # next rebuild re-projects the live client into the spec regardless.
        from kiro_crew.connections.oauth_clients import (  # noqa: PLC0415
            provider_for_server,
            strip_preregistered_oauth_client,
        )

        if provider_for_server(name, found) is not None:
            found = strip_preregistered_oauth_client(found)
        return found
    for path in (
        _kirocrew_mcp_json(),
        _GLOBAL_MCP_JSON,
        *[s.global_json for s in _extra_mcp_scopes()],
    ):
        found = _usable(_load_json_or_empty(path))
        if found is not None:
            return found
    return None


def _scope_has_entry(name: str, path: Path) -> bool:
    """Whether *path* holds an entry for the row *name* -- by the ONE key the row
    stands for (:func:`_config_entry_for`), so a raw ``npm:@...`` entry counts
    for its canonical row and an ambiguous name counts as absent."""
    return _config_entry_for(_load_json_or_empty(path).get("mcpServers", {}), name) is not None


def _set_kirocrew_entry(
    name: str,
    *,
    enabled: bool,
    spec: dict | None = None,
    preferred: str | None = None,
) -> str:
    """Set the server's ``disabled`` state in ``<data home>/mcp.json``.

    When ``enabled`` is True and ``spec`` is provided, upserts the full spec
    (used for preservation copies).  When enabled is False, adds/updates the
    entry to carry ``disabled: true`` — preserves existing command/args/env
    if already present; otherwise uses ``spec`` as the seed.

    ONE read-modify-write: the entry acted on is resolved from the read this
    call is about to write back (:func:`_resolve_key_from_read`), never from an
    earlier look at the file; *preferred* is the preflight's key and only breaks
    a tie the read itself cannot. The consent step the table offers on a
    ``disabledIn: "kirocrew"`` row sends the canonical ``playwright-mcp`` while
    the store may hold ``npm:@playwright/mcp`` -- lifting that entry in place is
    the consent; a canonical twin beside it would leave the muted raw entry
    standing and the row Disabled. A read that stays ambiguous writes nothing and
    answers ``"ambiguous"``.

    Returns a short label describing what happened: ``"added"``, ``"enabled"``,
    ``"disabled"``, ``"ambiguous"`` or ``"noop"``.
    """
    data = _load_json_or_empty(_kirocrew_mcp_json())
    servers = data.setdefault("mcpServers", {})
    try:
        key = _resolve_key_from_read(servers, name, preferred)
    except AmbiguousServerKey:
        return "ambiguous"
    existing = servers.get(key) if key is not None else None
    existing = existing if isinstance(existing, dict) else None

    if enabled:
        if existing is None and spec is None:
            return "noop"
        if existing is None:
            servers[name] = {k: v for k, v in (spec or {}).items() if k != "disabled"}
            action = "added"
        else:
            # Lift whatever MUTES the entry -- ``true`` or a non-boolean the
            # launch predicate reads fail-closed. Testing for the literal
            # ``True`` alone left ``"disabled": "false"`` in place as a "noop",
            # so the row stayed Disabled and the consent step could never land.
            if mcp_entry_is_muted(existing):
                existing.pop("disabled", None)
                action = "enabled"
            else:
                return "noop"
    else:
        if existing is None:
            base = spec or _find_server_spec_anywhere(name) or {}
            entry = {k: v for k, v in base.items() if k != "disabled"}
            entry["disabled"] = True
            servers[name] = entry
            action = "disabled"
        elif existing.get("disabled") is True:
            # The literal, not the predicate: a non-boolean already mutes the
            # entry, but the write below repairs it into the boolean the schema
            # wants, so only a real ``true`` is a no-op.
            return "noop"
        else:
            existing["disabled"] = True
            action = "disabled"

    _atomic_write(_kirocrew_mcp_json(), data)
    return action


def _get_kirocrew_entry(name: str) -> dict | None:
    """The raw entry for ``name`` from ``<data home>/mcp.json`` (or None).

    By the ONE key the row stands for (:func:`_config_key_for`): the table's
    Edit action sends the canonical ``playwright-mcp`` while the store may hold
    ``npm:@playwright/mcp`` -- the same entry ``kirocrewManaged`` was stamped
    from, so the action it advertises resolves. An ambiguous name reads as
    absent, like an unknown one.
    """
    data = _load_json_or_empty(_kirocrew_mcp_json())
    entry = _config_entry_for(data.get("mcpServers", {}), name)
    return dict(entry) if isinstance(entry, dict) else None


def _replace_kirocrew_spec(name: str, spec: dict) -> bool:
    """Replace the full spec of an existing Kiro Crew-managed entry.

    Preserves the entry's ``disabled`` state -- editing a spec is not
    consent to run it (mirrors the discover-install consent stance).
    Returns False when the name is not in ``<data home>/mcp.json`` (the
    caller decides how to report servers managed elsewhere). The entry is
    replaced under its OWN key (:func:`_config_key_for`), so a raw-keyed
    ``npm:@...`` entry edited through its canonical row stays one entry.
    """
    data = _load_json_or_empty(_kirocrew_mcp_json())
    servers = data.setdefault("mcpServers", {})
    try:
        key = _config_key_for(servers, name)
    except AmbiguousServerKey:
        return False
    existing = servers.get(key) if key is not None else None
    if key is None or not isinstance(existing, dict):
        return False
    entry = {k: v for k, v in spec.items() if k != "disabled"}
    # The launch predicate: an entry a non-boolean mutes stays muted through an
    # edit -- and comes out as the boolean the schema wants. Keeping only a
    # literal ``True`` would have dropped ``"disabled": "false"`` and turned a
    # spec edit into the consent it is not.
    if mcp_entry_is_muted(existing):
        entry["disabled"] = True
    servers[key] = entry
    _atomic_write(_kirocrew_mcp_json(), data)
    return True


def _rmw_remove_entry(path: Path, name: str, *, preferred: str | None = None) -> str:
    """Remove the server *name* from the ``mcpServers`` map at *path* in ONE
    read-modify-write, and say what happened.

    Read the file, compute the exact raw key to remove from THAT read
    (:func:`_resolve_key_from_read`), remove it, write -- one read and one write
    per store, nothing decided from an earlier look. Run under the MCP
    transaction lock (every uninstall caller holds it), so no dashboard writer
    can add, remove or re-alias an entry between the read and the write; the
    file's other entries go back exactly as read. ``"removed"`` on change,
    ``"noop"`` when the read holds nothing for the name, ``"ambiguous"`` when
    the read holds several raw keys for it and *preferred* is not one of them --
    then nothing is written, because one of them may be a distinct server.
    """
    data = _load_json_or_empty(path)
    servers = data.get("mcpServers", {})
    try:
        key = _resolve_key_from_read(servers, name, preferred)
    except AmbiguousServerKey:
        return "ambiguous"
    if key is None or not isinstance(servers, dict) or key not in servers:
        return "noop"
    del servers[key]
    _atomic_write(path, data)
    return "removed"


def _remove_kirocrew_entry(name: str, *, preferred: str | None = None) -> str:
    """Delete the server from ``<data home>/mcp.json`` -- :func:`_rmw_remove_entry`
    on the store. The row asks by its canonical alias, the file may hold the raw
    ``npm:@...`` spelling; the key comes from the read this call writes back."""
    return _rmw_remove_entry(_kirocrew_mcp_json(), name, preferred=preferred)


def _remove_from_agent_file(path: Path, name: str, *, preferred: str | None = None) -> bool:
    """Delete a server entry from a rendered agent file.

    Used by the uninstall path so the entry doesn't linger in
    ``~/.kiro/agents/kirocrew.json`` / ``~/.claude/agents/kirocrew.mcp.json``
    — the rebuild uses the existing agent file as its merge base, so without
    this targeted delete, additive merging would keep the entry alive.
    Returns True when the file was modified.

    Read AND write under THIS FILE's own sidecar lock (``bridges._mcp_lock``,
    ``<path>.lock``), which the MCP transaction lock every caller already holds
    does NOT cover: that one guards ``~/.kiro/settings/mcp.lock``, while
    ``apps/bridges_mcp.py`` does whole-file read-modify-writes of this same rendered
    file under ``~/.kiro/agents/kirocrew.lock`` (app enable/disable, MCP
    (de)registration). Unlocked, an app registration that read the file BEFORE
    this delete and wrote its whole map back AFTER resurrects the entry — and a
    caller that pairs the purge with a grant revoke (Disconnect) has by then
    unlinked the artifacts, leaving a configured provider with a dead
    credential. Nothing heals it: ``rebuild_agent_config`` takes this same file
    as its merge base and reconciles only ``app:server`` keys under that lock.

    Order is transaction-lock-then-file-lock, the same order every other
    settings-lock holder uses when it reaches a kirocrew.json writer, and no
    path in the tree takes the file lock first — so there is no ABBA cycle. The
    flock is blocking, which is legal here because every caller of
    :func:`_purge_server_config` runs it in a worker thread
    (``_offload_config_write``, or the sweep's executor), never on the event loop.

    ``bridges._mcp_lock`` is imported lazily: ``apps.bridges`` imports back into
    the dashboard handlers, so a module-level import is circular.
    """
    if not path.is_file():
        return False
    from kiro_crew.apps.bridges import _mcp_lock as _agent_file_lock

    with _agent_file_lock(target=path):
        return _rmw_remove_entry(path, name, preferred=preferred) == "removed"


def _set_scope_entry(
    path: Path,
    name: str,
    *,
    enabled: bool,
    spec: dict | None = None,
    preferred: str | None = None,
) -> str:
    """Add/remove a server from a provider global file (kiro or CC).

    When enabled=True and the server is absent, adds the spec.  When
    enabled=False, removes the entry entirely (NOT soft-disable — the
    dashboard badge treats absent and disabled identically).

    ONE read-modify-write: "the server" is the key :func:`_resolve_key_from_read`
    computes from the read this call writes back -- the caller passes the row's
    canonical alias, the file may hold the raw slash-named key -- and *preferred*
    (the preflight's key) only breaks a tie the read itself cannot. Enabling
    clears the mute on that entry; removing deletes it; neither adds a second,
    alias-keyed copy beside a raw-keyed one. A read that stays ambiguous returns
    ``"ambiguous"`` and writes nothing: one of those keys may be a distinct
    server.
    """
    data = _load_json_or_empty(path)
    servers = data.setdefault("mcpServers", {})
    try:
        key = _resolve_key_from_read(servers, name, preferred)
    except AmbiguousServerKey:
        return "ambiguous"
    entry = servers.get(key) if key is not None and isinstance(servers, dict) else None

    if enabled:
        if isinstance(entry, dict):
            # Already enabled; if the entry is muted (``true`` or a non-boolean
            # the launch predicate reads fail-closed), clear it.
            if mcp_entry_is_muted(entry):
                entry.pop("disabled", None)
                _atomic_write(path, data)
                return "enabled"
            return "noop"
        if spec is None:
            spec = _find_server_spec_anywhere(name)
        if spec is None:
            return "missing_spec"
        servers[name] = {k: v for k, v in spec.items() if k != "disabled"}
        _atomic_write(path, data)
        return "added"
    # enabled=False — hard remove.
    if key is None or not isinstance(entry, dict):
        return "noop"
    del servers[key]
    _atomic_write(path, data)
    return "removed"


def _set_tool_overrides(name: str, tool_overrides: dict[str, bool]) -> list[str]:
    """Apply per-tool enable/disable overrides to a server's entry in
    ``<data home>/mcp.json``.

    ``tool_overrides`` maps tool name → desired enabled state.  Disabled
    tools are added to the entry's ``disabledTools`` list; re-enabling
    removes them.  Creates the entry if absent (sourcing full spec from
    any scope so the server keeps loading).

    Returns a list of tool names whose state changed.
    """
    if not tool_overrides:
        return []
    data = _load_json_or_empty(_kirocrew_mcp_json())
    servers = data.setdefault("mcpServers", {})
    entry = servers.get(name)
    if not isinstance(entry, dict):
        # Seed from the best-available spec so the server keeps its config.
        base = _find_server_spec_anywhere(name) or {}
        entry = {k: v for k, v in base.items() if k != "disabled"}
        servers[name] = entry

    disabled = list(entry.get("disabledTools") or [])
    changed: list[str] = []
    for tool, tool_enabled in tool_overrides.items():
        if tool_enabled and tool in disabled:
            disabled.remove(tool)
            changed.append(tool)
        elif (not tool_enabled) and tool not in disabled:
            disabled.append(tool)
            changed.append(tool)

    if disabled:
        entry["disabledTools"] = disabled
    else:
        entry.pop("disabledTools", None)

    if changed:
        _atomic_write(_kirocrew_mcp_json(), data)
    return changed
