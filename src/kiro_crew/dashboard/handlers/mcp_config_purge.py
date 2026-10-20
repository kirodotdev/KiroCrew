"""Removing a server's config when its package is uninstalled.

The keys a change will touch, purging a server from every scope, scrubbing the copies of a
pre-registered OAuth client, sweeping uninstalls a cancelled apply left behind, and
forgetting the servers a connection owned.

Part of the ``kiro_crew.dashboard.handlers.mcp`` family: callers and tests reach every
name here as ``kiro_crew.dashboard.handlers.mcp.<name>``, and a patch there reaches this
module too (:mod:`kiro_crew._facade_mirror`). This module never imports the facade.
"""

from __future__ import annotations

import logging
from collections.abc import Collection
from pathlib import Path

from kiro_crew.config.paths import kiro_agents_dir
from kiro_crew.dashboard.handlers.mcp_config_entries import (
    _remove_from_agent_file,
    _remove_kirocrew_entry,
    _rmw_remove_entry,
)
from kiro_crew.dashboard.handlers.mcp_config_files import (
    _GLOBAL_MCP_JSON,
    AmbiguousServerKey,
    _atomic_write,
    _config_key_for,
    _extra_mcp_scopes,
    _get_mcp_lock_sync,
    _kirocrew_mcp_json,
    _load_json_or_empty,
)
from kiro_crew.mcp_discovery import SCOPE_KIRO_GLOBAL, SCOPE_KIROCREW
from kiro_crew.user_json import loads_user_json

logger = logging.getLogger("kiro_crew.dashboard.handlers.mcp")


async def _forget_owner_servers(names: list[str]) -> None:
    """Drop the owner sign-in records for servers the owner removed.

    Best effort, like the record on add: the removal already landed, and a stale
    record only matters if an entry with the same name AND URL comes back.
    """
    from kiro_crew.config.loader import update_config_locked
    from kiro_crew.connections.owner_servers import forget
    from kiro_crew.dashboard.chat_utils import run_config_write

    try:
        await run_config_write(update_config_locked, mutate=lambda cfg: forget(cfg, names))
    except Exception:  # noqa: BLE001 -- the removal itself already landed
        logger.warning("could not forget owner MCP servers %s", sorted(names), exc_info=True)


#: One change's preflight resolution: scope label -> the raw key that scope held
#: for the row when the request was checked (``None`` when it held nothing). A
#: TIEBREAKER for the writers, never an assertion of presence -- see
#: :func:`_resolve_key_from_read`.
PreflightKeys = dict[str, str | None]


def _uninstall_scope_files() -> list[tuple[str, Path]]:
    """Every (label, file) an uninstall writes, in purge order; labels are the
    ones :func:`_purge_server_config` reports."""
    scopes: list[tuple[str, Path]] = [
        (SCOPE_KIROCREW, _kirocrew_mcp_json()),
        (SCOPE_KIRO_GLOBAL, _GLOBAL_MCP_JSON),
        ("agent", kiro_agents_dir() / "kirocrew.json"),
    ]
    for scope in _extra_mcp_scopes():
        scopes.append((f"{scope.id}Global", scope.global_json))
        if scope.agent_mcp_file is not None:
            scopes.append((f"{scope.id}Agent", scope.agent_mcp_file))
    return scopes


def _preflight_change_keys(
    names: Collection[str],
) -> tuple[dict[str, PreflightKeys], dict[str, list[str]]]:
    """READ-ONLY check of every change before an apply's first side effect.

    An apply is two phases: companion package removal off the lock, then config
    writes under it. Each scope is read once here and each name resolved against
    that read (:func:`_config_key_for`); a name that is the alias of several raw
    keys with none exact in any scope is returned in the second mapping, and the
    caller refuses the WHOLE request -- before the package is gone, before any
    write. That is the only refusal an apply makes.

    What this returns is not trusted about presence later: the writers are each
    one read-modify-write that decides presence, absence and spelling from the
    read they write back (:func:`_rmw_remove_entry`, :func:`_set_scope_entry`,
    :func:`_set_kirocrew_entry`). The resolved key travels only as
    :func:`_resolve_key_from_read`'s tiebreaker for a colliding sibling that
    lands in between, so the entry the user's row stood for is the one acted on
    and the newcomer is left alone.

    Blocking file I/O: call through ``asyncio.to_thread``.
    """
    maps = [
        (label, _load_json_or_empty(path).get("mcpServers", {}))
        for label, path in _uninstall_scope_files()
    ]
    resolved: dict[str, PreflightKeys] = {}
    refused: dict[str, list[str]] = {}
    for name in names:
        keys: PreflightKeys = {}
        for label, servers in maps:
            try:
                keys[label] = _config_key_for(servers, name)
            except AmbiguousServerKey:
                refused.setdefault(name, []).append(label)
        resolved[name] = keys
    return resolved, refused


def _purge_server_config(
    name: str, *, scopes: Collection[str] | None = None, preferred: PreflightKeys | None = None
) -> dict[str, str]:
    """Remove a server's config from every scope + rendered agent file.

    The config-side half of an uninstall, factored out so the normal
    per-change path and the guaranteed-cleanup sweep (see ``api_mcp_apply``)
    run byte-identical removal logic. Idempotent: each store is ONE
    read-modify-write (:func:`_rmw_remove_entry`) that no-ops when the read holds
    nothing for the name, so running it a second time (e.g. the sweep re-purging
    a name the loop already handled) changes nothing. MUST be called under the
    MCP file lock, which is what makes each store's read and write one unit:
    nothing a dashboard writer adds, removes or re-aliases can land between them,
    and the file's other entries go back exactly as read. Returns the per-scope
    action labels for the response outcome.

    ``scopes`` restricts the purge to the named scopes -- the same labels this
    returns, which are also :func:`_load_mcp_json_by_source`'s keys, so a caller
    that judged ownership per scope acts on exactly the scopes it judged. ``None``
    means every scope, which is what an uninstall wants: the NAME is going away,
    so no scope may keep a definition of it. A caller that owns one ENDPOINT under
    a shared name must pass its scopes, because a same-named entry in another
    scope can be a different server whose config this must not delete.

    The rendered agent files are stripped whenever any scope was purged, and not
    at all otherwise: they are Kiro Crew's own merge output rather than a scope a
    user edits, and leaving the entry there lets the next rebuild resurrect what
    was just removed.

    Each store removes the ONE key its own read says the row stands for, never
    every key that aliases to the name: ``team/foo`` beside ``team-foo`` is two
    configurations, and an Uninstall of the row ``team-foo`` leaves ``team/foo``
    byte for byte. *preferred* (:func:`_preflight_change_keys`) only breaks the
    tie a colliding sibling creates in between; a read that stays ambiguous
    reports ``"ambiguous"`` for that store and writes nothing to it.
    """

    def _pref(label: str) -> str | None:
        return preferred.get(label) if preferred is not None else None

    actions: dict[str, str] = {}
    if scopes is None or SCOPE_KIROCREW in scopes:
        actions[SCOPE_KIROCREW] = _remove_kirocrew_entry(name, preferred=_pref(SCOPE_KIROCREW))
    if scopes is None or SCOPE_KIRO_GLOBAL in scopes:
        actions[SCOPE_KIRO_GLOBAL] = _rmw_remove_entry(
            _GLOBAL_MCP_JSON, name, preferred=_pref(SCOPE_KIRO_GLOBAL)
        )
    for scope in _extra_mcp_scopes():
        label = f"{scope.id}Global"
        if scopes is None or label in scopes:
            actions[label] = _rmw_remove_entry(scope.global_json, name, preferred=_pref(label))
    if not actions:
        return actions
    # Also strip the entry directly from the rendered agent files so the next
    # rebuild doesn't resurrect it via the "start from existing agent config"
    # base. Without this the additive merge keeps the entry around.
    _remove_from_agent_file(kiro_agents_dir() / "kirocrew.json", name, preferred=_pref("agent"))
    for scope in _extra_mcp_scopes():
        if scope.agent_mcp_file is not None:
            _remove_from_agent_file(scope.agent_mcp_file, name, preferred=_pref(f"{scope.id}Agent"))
    return actions


def _scrub_preregistered_oauth_copies(slug: str) -> dict[str, str]:
    """Strip a pre-registered provider's projected OAuth client from every source scope.

    The rendered agent spec carries the operator's client for ``slug`` (secret
    included). ``_find_server_spec_anywhere`` strips it when a scope toggle
    copies the render into a source scope, but a copy taken by hand from the
    rendered file, or one predating that rule, is a second home for the secret
    that a later removal or rotation would never reach -- so the OAuth-client
    mutation routes call this after the record changes. Only an entry that IS
    this provider (registry slug at the registry URL) is touched, and only its
    three owned keys; a same-named server pointing elsewhere, and every other
    key of the entry, are preserved. MUST be called under the MCP file lock.
    Returns the per-scope action labels. A scope that exists but cannot be read
    or parsed RAISES rather than counting as clean: the caller reports the
    mutation as committed-but-not-projected, since that file may still hold the
    retired secret.
    """
    from kiro_crew.connections.oauth_clients import (  # noqa: PLC0415
        provider_for_server,
        strip_preregistered_oauth_client,
    )

    actions: dict[str, str] = {}
    targets: list[tuple[str, Path]] = [
        (SCOPE_KIROCREW, _kirocrew_mcp_json()),
        (SCOPE_KIRO_GLOBAL, _GLOBAL_MCP_JSON),
        *[(f"{s.id}Global", s.global_json) for s in _extra_mcp_scopes()],
    ]
    for label, path in targets:
        # Not `_load_json_or_empty`: that reads a scope that cannot be opened or
        # parsed as EMPTY, which here would report "noop" for a file that may
        # still hold the retired secret. A scope that is absent is clean; one
        # that exists but cannot be read is a failure the caller surfaces.
        try:
            data = loads_user_json(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            actions[label] = "noop"
            continue
        if not isinstance(data, dict):
            raise ValueError(f"MCP scope {label} is not a JSON object; cannot scrub it")
        servers = data.get("mcpServers")
        if not isinstance(servers, dict):
            actions[label] = "noop"
            continue
        entry = servers.get(slug)
        if not isinstance(entry, dict) or provider_for_server(slug, entry) is None:
            actions[label] = "noop"
            continue
        stripped = strip_preregistered_oauth_client(entry)
        if stripped == entry:
            actions[label] = "noop"
            continue
        servers[slug] = stripped
        _atomic_write(path, data)
        actions[label] = "scrubbed"
    return actions


def _sweep_dangling_uninstalls(
    uninstall_names: list[str],
    purged_names: set[str],
    preferred: dict[str, PreflightKeys] | None = None,
) -> None:
    """Purge the config of every REQUESTED uninstall the apply loop did not reach.

    The compensation for package-first ordering: Phase 1 removes the companion
    package before Phase 2 removes its persisted config, so if the apply is
    interrupted between them (an earlier change's write raised, OR the request
    task was cancelled — e.g. gateway shutdown / client disconnect — during
    Phase 1, before the Phase-2 lock is ever taken) the config would dangle at
    an already-removed package.

    Sweeps by REQUEST, not by companion outcome. Phase 2's own uninstall branch
    removes config unconditionally for every ``uninstall`` change (it does not
    consult ``capability_results``), so the sweep mirrors that: any requested
    uninstall the loop did not reach (not in ``purged_names``) gets its config
    removed. Keying on the request — rather than on a ``capability_results`` entry
    that a cancellation could leave unset the instant AFTER the companion removed
    the package — closes the "package gone, result unrecorded, config kept"
    window at the root. Removing config for a requested uninstall is the user's
    intent anyway, and errs toward the BENIGN failure direction (config gone,
    package possibly orphaned-but-harmless — recoverable by reinstall) rather
    than the harmful one (config kept, package gone → broken at every session).

    SYNCHRONOUS by design and dispatched to a worker thread by the caller (the
    ``api_mcp_apply`` finally does ``run_in_executor`` + awaits the future to
    completion, shielding it across cancellation). Running the whole
    acquire→purge→release off the event loop is what makes it both
    deadlock-free (the loop stays free so a task holding the MCP lock can release
    it) and run-to-completion (a worker thread is not cancelled when the request
    task is). It uses its OWN synchronous lock, so it is correct whether or not
    the caller still holds the async lock (the Phase-2 ``async with`` has already
    exited by the time we reach here, and on a Phase-1 cancel it was never taken).
    Idempotent (``_purge_server_config`` no-ops on absent entries), so a name
    already purged by the loop (in ``purged_names``) is skipped and a re-run is
    cheap. With *preferred* (:func:`_preflight_change_keys`, taken before Phase 1) each
    name's purge carries the same tiebreaker the loop would have; presence is the read's.
    """
    to_sweep = [n for n in uninstall_names if n not in purged_names]
    if not to_sweep:
        return
    # Best-effort: this runs from the apply's finally, so it must NEVER raise —
    # a failure here (even the lock acquire) would replace a successful apply's
    # response with a 500, or mask the original exception being unwound. Guard
    # the WHOLE body (lock acquire included), not just each purge; a miss self-
    # heals on the next idempotent apply.
    try:
        with _get_mcp_lock_sync():
            for uname in to_sweep:
                _purge_server_config(uname, preferred=(preferred or {}).get(uname))
                logger.warning(
                    "mcp apply interrupted before purging config for requested "
                    "uninstall %r; swept it in guaranteed cleanup to avoid a "
                    "dangling config→removed-package reference",
                    uname,
                )
    except Exception:
        logger.exception(
            "guaranteed-cleanup sweep failed (%s); config may reference a removed "
            "package until the next apply",
            to_sweep,
        )
