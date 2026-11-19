"""Copying MCP servers into the Kiro Crew agent spec.

``_sync_mcp_to_agent`` and its batch form, the refusal when the agent home is not ours to
write, the refusal to switch off or remove one of Kiro Crew's own servers (one route's
name, an apply batch, the toggle-all skip), and whether live sessions pick a change up by
hot reload.

Part of the ``kiro_crew.dashboard.handlers.mcp`` family: callers and tests reach every
name here as ``kiro_crew.dashboard.handlers.mcp.<name>``, and a patch there reaches this
module too (:mod:`kiro_crew._facade_mirror`). This module never imports the facade.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

from aiohttp import web

from kiro_crew.agent import (
    AGENT_FILENAME,
    SharedAgentHomeRefused,
    _atomic_json_write,
    _declined_foreign_spec_write,
    emission_eligible_mcp_servers,
    entry_as_enabled,
    kiro_agents_dir_path,
)
from kiro_crew.dashboard.handlers.mcp_config_entries import _kirocrew_entry_is_muted
from kiro_crew.dashboard.handlers.mcp_config_files import _GLOBAL_MCP_JSON
from kiro_crew.mcp_hot_reload import live_sessions_apply_mcp_live, mark_reinjecting_providers
from kiro_crew.mcp_provenance import WITHHELD_KEY
from kiro_crew.mcp_utils import mcp_server_alias, without_mcp_refs
from kiro_crew.platform.governance import may_skip_gate_now
from kiro_crew.sel import sel
from kiro_crew.user_json import loads_mcp_config

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from contextlib import AbstractAsyncContextManager

logger = logging.getLogger("kiro_crew.dashboard.handlers.mcp")


def _agent_home_not_owned_response(name: str) -> web.Response:
    """409 for a sync this instance's data home is not allowed to write.

    Mirrors ``api_agent_detail``'s arm. The spec write was refused, so whatever
    the caller asked to change is still defined in the file its own sessions
    load: answering ``ok`` would tell the operator a server was disabled or
    deleted while the agent keeps it mounted with its ``autoApprove`` intact.
    The remedy is the existing non-default-home one -- remove the stale
    ``kirocrew*.json`` specs and restart -- and the gateway log names the
    directory. Deliberately NOT ``KIRO_HOME``: see :func:`config.paths.kiro_home`'s
    scope caveat, which is why the rebuild's own refusal suggests no remedy either.
    """
    return web.json_response(
        {
            "error": (
                f"'{name}' was not applied: this agent config is owned by another "
                "Kiro Crew data home, which this instance will not rewrite."
            ),
            "code": "agent_home_not_owned",
        },
        status=409,
    )


def _shared_agent_home_declined() -> bool:
    """Whether this data home may NOT write the shared agent specs.

    The one question the pre-mutation preflight asks, kept as its own function so
    the preflight and the write cannot answer differently for reasons other than
    time. Asked through ``_declined_foreign_spec_write`` against the very file
    those writers write, which also warns and audits the decision.
    """
    return _declined_foreign_spec_write(kiro_agents_dir_path() / AGENT_FILENAME)


def _agent_home_not_owned(name: str) -> web.Response | None:
    """The same 409, decided BEFORE the first mutation; ``None`` to proceed.

    The arms around each ``_atomic_json_write`` catch a refusal that has
    already been reached, which is the right guard for the write itself but
    arrives too late for a handler that mutates something else first. The
    uninstall path is the clear case: it deletes the server from the data
    home's registry, the user-level registry and every companion scope, and
    only then writes the rendered spec -- so a refusal there leaves the server
    gone from every source while the spec its own sessions load still mounts
    it with its ``autoApprove``, and this instance's rebuild is refused too,
    so nothing reconciles it.

    Asked through ``_declined_foreign_spec_write`` against the very file these
    handlers write, so the preflight and the write cannot disagree, and the
    denial is warned and audited once here instead of once at the write.
    """
    if not _shared_agent_home_declined():
        return None
    return _agent_home_not_owned_response(name)


def _builtin_mcp_server_names() -> frozenset[str]:
    """The MCP servers no route may switch off or remove: what a fresh spec build would
    emit right now (:func:`emission_eligible_mcp_servers`), so every ``opt_in`` set is
    left out and the lock and the rebuild read one predicate.

    Blocking, because the edition seam it reads may compose the platform context, so a
    caller on the event loop offloads it. A host whose context cannot compose (where
    the agent rebuild refuses too) answers with no names: every route then answers as
    it does without this rule, rather than a 500.
    """
    try:
        return emission_eligible_mcp_servers()
    except Exception:
        logger.warning("MCP server set unreadable; no built-in server is locked", exc_info=True)
        return frozenset()


async def _is_builtin_mcp_server(name: str) -> bool:
    """Whether *name*, or the alias it is listed under, is one of Kiro Crew's own servers.

    A raw key such as ``@kirocrew/core`` is listed as its alias ``kirocrew-core``, so
    both spellings are tested against :func:`_builtin_mcp_server_names`, read off the
    loop on each call (it is not cached, so an edition change is seen at once).
    """
    names = {name, mcp_server_alias(name)}
    return bool(names & await asyncio.to_thread(_builtin_mcp_server_names))


async def _builtin_server_refusal(name: str) -> web.Response | None:
    """A 409 when *name* is one of Kiro Crew's own MCP servers, else ``None``.

    Those servers (``kirocrew-core``, ``kirocrew-cron``, ``kirocrew-guide`` and the
    rest) carry the tools Kiro Crew itself runs on, so no route may switch one off or
    remove it. Callers ask only for the off and remove directions: turning one back on
    stays accepted, so a built-in server that is already off can be restored. The
    name test is :func:`_is_builtin_mcp_server`.
    """
    if not await _is_builtin_mcp_server(name):
        return None
    sel().log_api_access(
        caller="dashboard",
        operation="mcp_builtin_disable_refused",
        outcome="denied",
        resources=name[:64],
    )
    return web.json_response(
        {
            "error": f"{name!r} is built into Kiro Crew and cannot be turned off or removed",
            "code": "mcp_server_builtin",
        },
        status=409,
    )


async def _toggle_all_skip(enabled: bool) -> Callable[[str], bool]:
    """Which raw keys a toggle-all leaves alone.

    Disabling all never reaches Kiro Crew's own servers, by name or by the alias the
    key is listed under; enabling all still lifts a flag one of them already carries.
    """
    if enabled:
        return lambda _name: False
    builtin = await asyncio.to_thread(_builtin_mcp_server_names)
    return lambda name: name in builtin or mcp_server_alias(name) in builtin


async def _builtin_apply_refusal(
    changes: Iterable[Any], lock: Callable[[], AbstractAsyncContextManager[Any]]
) -> web.Response | None:
    """The 409 for an apply batch that turns off or uninstalls a built-in, else ``None``.

    Taken for the whole request before the apply's Phase 1, which uninstalls companion
    packages ahead of every scope write. The off test is the one Phase 2 applies (an
    absent ``kirocrew`` means on), and it counts only a change that turns the server
    off: the table sends a row's current presence with every edit, so a built-in the
    store already mutes keeps its tool and Globals edits. That store read is taken only
    for a built-in name, under *lock*, the MCP config file lock the caller holds for its
    writes; a third-party change never takes it.
    """
    for change in changes:
        if not isinstance(change, dict):
            continue
        name = str(change.get("name", "")).strip()
        uninstall = bool(change.get("uninstall"))
        if not uninstall and bool(change.get("kirocrew", True)):
            continue
        if not await _is_builtin_mcp_server(name):
            continue
        if not uninstall:
            async with lock():
                if await asyncio.to_thread(_kirocrew_entry_is_muted, name):
                    continue
        refused = await _builtin_server_refusal(name)
        if refused is not None:
            return refused
    return None


async def _builtin_stays_on(name: str) -> bool:
    """Whether an apply that asks to turn *name* off must keep it on.

    True for a built-in the store does not mute now: another apply lifted the mute after
    :func:`_builtin_apply_refusal` read it. Called under the MCP config file lock, so
    the read and the apply's write see the same file.
    """
    return await _is_builtin_mcp_server(name) and not await asyncio.to_thread(
        _kirocrew_entry_is_muted, name
    )


def _sync_mcp_to_agent(name: str, enabled: bool, *, remove: bool = False) -> None:
    """Serialize the kirocrew.json read-modify-write with bridges' app-MCP
    registration. Both do a FULL RMW of the same file; bridges holds `_mcp_lock`
    while this dashboard path held only the in-process `_get_config_lock` — a
    DIFFERENT lock — so a concurrent app (re)registration and a dashboard toggle
    could each read the file and the last write drop the other's change. Lock the
    file actually being written (== `_mcp_json_path()` in production, the patched
    tmp path under test) so the two paths serialize on one lock.
    """
    from kiro_crew.apps.bridges import _mcp_lock
    from kiro_crew.dashboard.handlers.agents import _installed_agent_config

    with _mcp_lock(target=_installed_agent_config()):
        _sync_mcp_to_agent_unlocked(name, enabled, remove=remove)


def _sync_mcp_to_agent_unlocked(name: str, enabled: bool, *, remove: bool = False) -> None:
    """Sync MCP server state to kirocrew.json mcpServers (not tools/allowedTools)."""
    from kiro_crew.atomic_write import read_bytes_with_retry
    from kiro_crew.dashboard.handlers.agents import (  # noqa: F811 circular: agents imports mcp
        _installed_agent_config,
    )

    path = _installed_agent_config()
    try:
        cfg = loads_mcp_config(read_bytes_with_retry(path).decode("utf-8"))
    except (FileNotFoundError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        logger.warning("Cannot read agent config %s, skipping sync: %s", path, exc)
        return

    alias = mcp_server_alias(name)
    if enabled and not remove:
        # Ensure server exists in kirocrew.json mcpServers when enabled
        mcp_servers = cfg.setdefault("mcpServers", {})
        tool_ref = f"@{alias}"
        changed = False
        if alias not in mcp_servers:
            # Copy spec from global mcp.json (looked up by original name)
            try:
                gdata = loads_mcp_config(_GLOBAL_MCP_JSON.read_text(encoding="utf-8"), jsonc=True)
                spec = gdata.get("mcpServers", {}).get(name, {})
                if isinstance(spec, dict) and spec:
                    entry = {k: v for k, v in spec.items() if k != "disabled"}
                    # Strip a governed `autoApprove`: a verb it keeps on the copied
                    # entry is granted in `allowedTools` (the skill view on kiro-cli,
                    # the projection on KAS) and runs WITHOUT reaching the
                    # PreToolUse gate — the same exemption `allowedTools` is
                    # withheld for below. Drop it when the ceiling constrains this
                    # server.
                    if "autoApprove" in entry and not may_skip_gate_now(tool_ref):
                        entry.pop("autoApprove", None)
                    mcp_servers[alias] = entry
                    changed = True
                else:
                    return
            # The user's hand-edited file: unreadable (permissions) or not UTF-8
            # (a legacy code-page save) means no spec to copy, same as absent.
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                return
        # Strip a governed `autoApprove` from the entry REGARDLESS of whether the
        # alias was just copied or already existed: a re-enable, or a spec written
        # before the ceiling arrived, skips the copy branch above and would keep
        # its gate exemption otherwise.
        _existing = mcp_servers.get(alias)
        if (
            isinstance(_existing, dict)
            and "autoApprove" in _existing
            and not may_skip_gate_now(tool_ref)
        ):
            _existing.pop("autoApprove", None)
            changed = True
        # A re-enable lifts the ``disabled`` the disable path below wrote onto
        # this entry -- but not the mute the rebuild writes on a server whose
        # ``secret://`` reference nothing here would resolve: lifting that one
        # starts the server with the literal reference, and no rebuild runs after
        # this write to put it back. ``entry_as_enabled`` takes the rebuild's own
        # decision for the entry, so a freshly copied one is asked as well.
        if isinstance(_existing, dict):
            _enabled = entry_as_enabled(alias, _existing)
            if _enabled != _existing:
                mcp_servers[alias] = _enabled
                changed = True
        # Ensure @server-name in tools, and in allowedTools only if the
        # governance ceiling has nothing to say about this server. `tools` MOUNTS
        # it; `allowedTools` additionally auto-approves it, and auto-approve is
        # the one path that never reaches the PreToolUse gate — so granting it
        # unconditionally here made the ceiling un-enforceable for any server a
        # user enables from the dashboard, which is the common case.
        keys = ("tools", "allowedTools") if may_skip_gate_now(tool_ref) else ("tools",)
        for key in keys:
            if cfg.get(key) is None:  # a hand-edited null reads as no list yet
                cfg[key] = []
            lst = cfg[key]
            if tool_ref not in lst:
                lst.append(tool_ref)
                changed = True
        if "allowedTools" not in keys:
            stale = cfg.get("allowedTools")
            if isinstance(stale, list):
                # A grant written before the ceiling arrived must not survive it,
                # in either spelling.
                kept = without_mcp_refs(stale, (alias,))
                if len(kept) != len(stale):
                    stale[:] = kept
                    changed = True
        if not changed:
            return
        if "allowedTools" not in keys:
            # Governed: the ref was mounted in `tools` but auto-approve was
            # WITHHELD from allowedTools (and any stale grant/autoApprove
            # removed) because the ceiling constrains this server. Record the
            # withheld decision — emitting mcp_tools_added here would falsely
            # report that auto-approve was granted.
            sel().log_api_access(
                caller="system",
                operation="mcp_auto_approve_withheld",
                outcome="ok",
                source="dashboard",
                resources=f"{tool_ref} mounted in tools; auto-approve withheld (ceiling)",
            )
        else:
            sel().log_api_access(
                caller="system",
                operation="mcp_tools_added",
                outcome="ok",
                source="dashboard",
                resources=f"{tool_ref} added to tools/allowedTools",
            )
    # On disable/remove, clean up any @server-name refs the user may have added
    if not enabled or remove:
        stale_servers = (alias, name)
        tool_ref = f"@{alias}"
        cfg["tools"] = without_mcp_refs(cfg.get("tools", []), stale_servers, per_tool=False)
        # Per-tool grants go too: ``@server/tool`` in allowedTools auto-approves
        # that tool just as the bare ref does for the whole server.
        cfg["allowedTools"] = without_mcp_refs(cfg.get("allowedTools", []), stale_servers)
        sel().log_api_access(
            caller="system",
            operation="mcp_tools_removed",
            outcome="ok",
            source="dashboard",
            resources=f"{tool_ref} removed from tools/allowedTools",
        )
    if not enabled and not remove:
        # Dropping the ref unmounts the tools only for a session that has not
        # started yet. kiro-cli is what reads this file, and its live reconcile
        # (see :mod:`kiro_crew.mcp_hot_reload`) leaves a still-running server's
        # tools mounted when only the ref goes — but stops the process for an
        # entry marked ``disabled``. The marker also spares a cold session the
        # spawn of a server nothing mounts. The ``disabled`` in the kiro-global
        # file cannot stand in: ``includeMcpJson`` is pinned false, so kiro-cli
        # never reads it.
        _mark_agent_entries_disabled(cfg, (alias, name))
    if remove:
        cfg.get("mcpServers", {}).pop(alias, None)
        cfg.get("mcpServers", {}).pop(name, None)
    try:
        _atomic_json_write(path, cfg)
    except SharedAgentHomeRefused:
        # Not a transient write failure: the entry was already popped from the
        # in-memory cfg, so swallowing this would report a removal (or a
        # disable) the spec on disk never took, leaving the server mounted with
        # whatever autoApprove it carried. The caller turns it into a 409.
        raise
    except OSError as exc:
        # Raised, not swallowed: the caller would answer ``applied`` while
        # kiro-cli still loads the old spec. Same reason the read raises.
        logger.warning("Cannot write agent config %s: %s", path, exc)
        raise


def _mark_agent_entries_disabled(cfg: dict, keys: tuple[str, ...]) -> bool:
    """Set ``disabled: true`` on each present ``mcpServers`` entry named in ``keys``.

    A key may name the alias or the legacy slash form of one server; both are
    marked when both exist so neither spawns. Only a mapping entry can carry the
    flag — a string/null entry is left as-is. Returns True when anything changed.

    The rebuild's withheld record goes too: once the user switches the server
    off, the mute is theirs, and a record left beside it would let the rebuild
    lift it as soon as the server is routed.
    """
    servers = cfg.get("mcpServers")
    if not isinstance(servers, dict):
        return False
    changed = False
    for key in keys:
        entry = servers.get(key)
        if not isinstance(entry, dict):
            continue
        if entry.pop(WITHHELD_KEY, None) is not None:
            changed = True
        if entry.get("disabled") is not True:
            entry["disabled"] = True
            changed = True
    return changed


def _sync_mcp_to_agent_batch(names: list[str], enabled: bool) -> None:
    """See :func:`_sync_mcp_to_agent` — the same file lock around the batch RMW."""
    from kiro_crew.apps.bridges import _mcp_lock
    from kiro_crew.dashboard.handlers.agents import _installed_agent_config

    with _mcp_lock(target=_installed_agent_config()):
        _sync_mcp_to_agent_batch_unlocked(names, enabled)


def _sync_mcp_to_agent_batch_unlocked(names: list[str], enabled: bool) -> None:
    """Batch sync multiple MCP servers to kirocrew.json in a single read-modify-write."""
    from kiro_crew.atomic_write import read_bytes_with_retry
    from kiro_crew.dashboard.handlers.agents import (  # noqa: F811 circular: agents imports mcp
        _installed_agent_config,
    )

    path = _installed_agent_config()
    try:
        cfg = loads_mcp_config(read_bytes_with_retry(path).decode("utf-8"))
    except (FileNotFoundError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        logger.warning("Cannot read agent config %s, skipping batch sync: %s", path, exc)
        return

    changed = False
    if enabled:
        # Ensure all servers exist in kirocrew.json mcpServers
        mcp_servers = cfg.setdefault("mcpServers", {})
        try:
            gdata = loads_mcp_config(_GLOBAL_MCP_JSON.read_text(encoding="utf-8"), jsonc=True)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):  # see the single path
            gdata = {}
        granted_refs: list[str] = []
        withheld_refs: list[str] = []
        for name in names:
            alias = mcp_server_alias(name)
            if alias not in mcp_servers:
                spec = gdata.get("mcpServers", {}).get(name, {})
                if not isinstance(spec, dict) or not spec:
                    continue
                _entry = {k: v for k, v in spec.items() if k != "disabled"}
                # Strip a governed autoApprove — see the single-server path.
                if "autoApprove" in _entry and not may_skip_gate_now(f"@{alias}"):
                    _entry.pop("autoApprove", None)
                mcp_servers[alias] = _entry
                changed = True
            # Strip a governed autoApprove REGARDLESS of whether the alias was just
            # copied or already existed — see the single-server path.
            _existing = mcp_servers.get(alias)
            if (
                isinstance(_existing, dict)
                and "autoApprove" in _existing
                and not may_skip_gate_now(f"@{alias}")
            ):
                _existing.pop("autoApprove", None)
                changed = True
            # Lift the ``disabled`` a batch disable wrote, keeping the rebuild's
            # withheld mute -- see the single-server path.
            if isinstance(_existing, dict):
                _enabled = entry_as_enabled(alias, _existing)
                if _enabled != _existing:
                    mcp_servers[alias] = _enabled
                    changed = True
            # Same split as the single-server path above: mount always,
            # auto-approve only when the ceiling is silent about this server.
            tool_ref = f"@{alias}"
            keys = ("tools", "allowedTools") if may_skip_gate_now(tool_ref) else ("tools",)
            if "allowedTools" in keys:
                granted_refs.append(tool_ref)
            else:
                withheld_refs.append(tool_ref)
            for key in keys:
                if cfg.get(key) is None:  # a hand-edited null reads as no list yet
                    cfg[key] = []
                lst = cfg[key]
                if tool_ref not in lst:
                    lst.append(tool_ref)
                    changed = True
            if "allowedTools" not in keys:
                stale = cfg.get("allowedTools")
                if isinstance(stale, list):
                    kept = without_mcp_refs(stale, (alias,))
                    if len(kept) != len(stale):
                        stale[:] = kept
                        changed = True
        if changed:
            if granted_refs:
                sel().log_api_access(
                    caller="system",
                    operation="mcp_tools_added",
                    outcome="ok",
                    source="dashboard",
                    resources=f"{', '.join(granted_refs)} added to tools/allowedTools",
                )
            if withheld_refs:
                # Governed refs: mounted in `tools` but auto-approve withheld
                # from allowedTools. Record the withheld decision rather than a
                # grant, matching the single-server path.
                sel().log_api_access(
                    caller="system",
                    operation="mcp_auto_approve_withheld",
                    outcome="ok",
                    source="dashboard",
                    resources=(
                        f"{', '.join(withheld_refs)} mounted in tools; "
                        "auto-approve withheld (ceiling)"
                    ),
                )
    else:
        # Remove both the alias ref and any legacy slash ref the user may have.
        refs_to_remove = {f"@{name}" for name in names} | {
            f"@{mcp_server_alias(name)}" for name in names
        }
        servers_to_remove = tuple(names) + tuple(mcp_server_alias(name) for name in names)
        cfg["tools"] = without_mcp_refs(cfg.get("tools", []), servers_to_remove, per_tool=False)
        cfg["allowedTools"] = without_mcp_refs(cfg.get("allowedTools", []), servers_to_remove)
        # Mark the entries too — a dropped ref alone does not stop a server that
        # a live session is already running (see the single-server path).
        _mark_agent_entries_disabled(cfg, servers_to_remove)
        changed = True
        sel().log_api_access(
            caller="system",
            operation="mcp_tools_removed",
            outcome="ok",
            source="dashboard",
            resources=f"{', '.join(sorted(refs_to_remove))} removed from tools/allowedTools",
        )
    if not changed:
        return
    try:
        _atomic_json_write(path, cfg)
    except SharedAgentHomeRefused:
        # See the single-server sync: the batch already mutated cfg in memory,
        # so a swallowed refusal reports every name in it as applied.
        raise
    except OSError as exc:
        # Raised, not swallowed: the caller would answer ``applied`` while
        # kiro-cli still loads the old spec. Same reason the read raises.
        logger.warning("Cannot write agent config %s: %s", path, exc)
        raise


def _mcp_hot_reload_active(request: web.Request) -> bool:
    """Whether every live session applies MCP config edits without a reset.

    Three mechanisms satisfy it, per provider: the process reconciles the agent
    file itself (kiro-cli, ``mcp_config_hot_reload``), Crew re-sends the
    session's projection at its next turn boundary (KAS,
    ``mcp_config_wire_reinject``), or Crew relaunches the process and reloads the
    session at its next turn boundary (Strands, ``mcp_config_relaunch_reload``). Keyed to the processes actually running — the
    registered sessions plus the warm pool the reset would drain — and, for the
    first mechanism, to the version each reported at its own handshake, never
    to the binary on disk (which is newer than every live process after an
    in-place upgrade). Fails CLOSED: any error answers False, and the caller
    falls back to the reset that was always correct — a skipped reset is the
    one outcome a user cannot see.

    A True answer is also the moment the re-injecting and relaunching providers are MARKED:
    nothing in their engine watches the file, so the skip is only correct if
    each of them is told a change is due. Marking is a flag per provider, never
    on the wire, so it runs here under the writer's own locks.
    """
    try:
        sessions = request.app["state"].sessions
        providers = list(sessions.active_providers()) + list(sessions.warm_providers())
        if not live_sessions_apply_mcp_live(providers):
            return False
        marked = mark_reinjecting_providers(providers)
        # A warm-pool fill between its start and its enqueue is in neither
        # snapshot; the pool marks it at the enqueue from this fence.
        fence = getattr(sessions, "mark_mcp_config_changed", None)
        if callable(fence):
            fence()
        if marked:
            logger.info(
                "MCP config changed: %d live session(s) will apply it at their next "
                "turn instead of being reset",
                marked,
            )
        return True
    except Exception:
        logger.warning("MCP hot-reload gate failed; resetting sessions instead", exc_info=True)
        return False
