"""Installed kiro-cli agents: ``GET /api/agents/installed`` and ``POST /api/agents/sync``, which enrolls installed templates into ``config.json``."""

from __future__ import annotations

import asyncio
import dataclasses
from typing import TYPE_CHECKING, Any

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.handlers.agents import (
        KiroCrewAgentConfig,
        KiroCrewConfig,
        UnknownMemoryStore,
        _drained_to_thread,
        _get_config_lock,
        _name_would_be_masked,
        _read_agent_spec,
        _require_owner,
        _sel,
        _spec_stem_on_disk,
        coerce_dict_section,
        discovery_executor,
        kiro_agents_dir_path,
        list_agents,
        logger,
        memory_store_namespace_lock,
        teams_mod,
        update_config_locked,
    )


def _namespaced_agent_file_exists(agent_name: str) -> bool:
    """True when an app-registered agent file backs *agent_name*.

    App agents are materialized as ``<app>--<agent>.json`` (namespaced file
    names prevent two apps' same-named agents from clobbering each other), but
    kiro-cli resolves agents by the JSON ``name`` field, not the file name. A
    file-name-only existence check therefore reports a perfectly spawnable app
    agent as missing on every boot.
    """
    # Resolved per call, not read from a module constant: the agents dir tracks
    # the live data home (see config.md "Data Home"), and a frozen constant would
    # glob the real ~/.kiro from an isolated run.
    try:
        for path in kiro_agents_dir_path().glob(f"*--{agent_name}.json"):
            data = _read_agent_spec(
                path,
                operation="api_agents_sync",
                source="dashboard",
            )
            if data is None:
                continue
            if data.get("name") == agent_name:
                return True
    except OSError:
        return False
    return False


async def api_agents_installed(request: web.Request) -> web.Response:
    """GET /api/agents/installed — list all installed kiro-cli agents.

    kirocrew is always first; kirocrew-lite is excluded.

    Deliberately GLOBAL-only (no project scope): every frontend consumer of this
    endpoint is an agent CRUD/editor surface (Agents page, template editor) whose
    actions persist into the global configuration — "Set as default" writes the
    selected name into ``cfg.agents``. A project-scope row here would let that
    action persist a name that exists only inside one checkout, producing a
    default agent the config cannot resolve. Project-scope discovery instead
    reaches the surfaces that DISPATCH agents: per-turn resolution
    (``resolve_agent_bindings(..., project_dir=...)``), spawn validation, and
    Slack — see ``agent_discovery.project_agent_names``.
    """

    # list_agents() does glob + per-file resolve(strict=True) + read_bytes +
    # json.loads over ~/.kiro/agents — blocking filesystem work that, on a large
    # agents dir (network home, many project-registry agents), can stall the
    # event loop past the loop-stall watchdog when a browser loads the dashboard.
    # Offload to the discovery pool, same as /api/skills.
    def _collect() -> list[Any]:
        agents = list(list_agents())
        agents.sort(key=lambda a: (0 if a.name == "kirocrew" else 1, a.name))
        return agents

    agents = await asyncio.get_running_loop().run_in_executor(discovery_executor(), _collect)
    return web.json_response([a.to_dict() for a in agents])


async def api_kirocrew_agents_sync(request: web.Request) -> web.Response:
    """POST /api/agents/sync — auto-sync AIM-installed agents into config.json."""
    denied = await _require_owner(request, "agents.sync")
    if denied is not None:
        return denied
    async with _get_config_lock():
        return await _do_agents_sync(request)


async def _do_agents_sync(request: web.Request) -> web.Response:

    cfg = KiroCrewConfig.load()
    synced: list[str] = []
    pruned: list[str] = []
    prune_candidates: dict[str, dict] = {}
    try:
        discovered_agents = await asyncio.get_running_loop().run_in_executor(
            discovery_executor(), lambda: list(list_agents())
        )
        discovered_names = {a.name for a in discovered_agents}

        # Add new agents
        mc_kiro_agents = {a.kiro_agent for a in cfg.agents.values()}
        for disc in discovered_agents:
            if (
                disc.name not in mc_kiro_agents
                and disc.name not in cfg.agents
                and disc.source != "kirocrew"
                # A fork is one crew's private copy, not a standalone template:
                # normally its owner's binding puts it in mc_kiro_agents, so this
                # only fires for an ORPHANED copy (owner crew deleted) — which
                # must not resurrect as a ghost agent.
                and not disc.private_to
            ):
                # EXECUTABLE INVARIANT enforcement (mirrors the seam-boundary
                # LIVENESS bound in platform.capability_bound —
                # BoundedCapabilityManager): a builtin_agents() row MUST be
                # spawnable. The core can only verify the on-disk case
                # (~/.kiro/agents/<name>.json); an edition may also make a
                # row ACP-resolvable WITHOUT an on-disk file, so we WARN rather
                # than hard-drop — dropping a legitimately ACP-resolvable agent
                # would itself be a correctness bug. The warning turns an
                # otherwise silent spawn-time (ACP session/set_mode) failure into
                # an actionable log line pointing at the offending seam row.
                # Upstream resolves the agents dir per call (data-home safety);
                # the namespaced check is for app-provided agents, which live as
                # `<app>--<agent>.json` and would otherwise look "missing".
                # Off the loop: both the stat and the namespaced glob touch the
                # filesystem, and on a populated agents directory this per-agent
                # check (in a loop) would stall the gateway loop and heartbeat.
                _dn = disc.name
                # The OTHER way a name reaches `cfg.agents`, and the one the
                # create-route check cannot see. A discovered spec's name is
                # package-controlled rather than typed by the owner, so "the owner
                # is reading a string the owner wrote" does not hold for it: a
                # package could land a credential-shaped name that then reaches
                # the roster. Refused here, at the second source, for the same
                # reason it is refused at the first.
                #
                # Skipped rather than masked: masking would leave an
                # unselectable, unrenameable row, and this row has no owner to
                # rename it -- it comes back on every sync until the PACKAGE is
                # fixed. The name is deliberately absent from the log line, since
                # writing it into the log is the disclosure being avoided.
                if _name_would_be_masked(_dn):
                    logger.warning(
                        "refusing to sync a discovered agent whose name is "
                        "credential- or URL-shaped (source=%s); name withheld "
                        "from this log deliberately -- fix the providing package",
                        getattr(disc, "source", "?"),
                    )
                    continue
                _has_on_disk = await asyncio.to_thread(
                    lambda: _spec_stem_on_disk(kiro_agents_dir_path(), _dn)
                    or _namespaced_agent_file_exists(_dn)
                )
                if not _has_on_disk:
                    logger.warning(
                        "syncing agent %r (source=%s) with no on-disk config at "
                        "%s — if it is not ACP-resolvable it will persist into "
                        "config.json and fail at spawn (builtin_agents EXECUTABLE "
                        "INVARIANT)",
                        disc.name,
                        disc.source,
                        kiro_agents_dir_path() / f"{disc.name}.json",
                    )
                cfg.agents[disc.name] = KiroCrewAgentConfig(
                    kiro_agent=disc.name,
                    description=disc.description,
                    source=disc.source,
                )
                # Discovery registers a template; member creation initializes memory.
                # The owner can opt in through the existing member update action.
                synced.append(disc.name)

        # Prune agents whose kiro_agent file is missing on disk.
        # Only prune package-installed agents (never user-created or kirocrew-owned).
        # Skip pruning if scan returned nothing -- likely a transient issue.
        # Invariant: for package-sourced entries, kiro_agent == dict key == agent name.
        # ("aim" is also accepted for backward-compat with older configs.)
        # A STARRED package crew is pruned like any other -- a registry row with
        # no spec on disk is not spawnable. The star goes with the row: a
        # reinstalled crew comes back un-starred and one click restores it
        # (deliberately no parking list -- a permanent config key is not worth
        # a re-click, and a name-keyed list would pre-star an unrelated future
        # package that reused the name).
        if discovered_names:
            for name, agent_cfg in list(cfg.agents.items()):
                if agent_cfg.source in ("package", "aim") and (
                    agent_cfg.kiro_agent not in discovered_names
                ):
                    # Record the SNAPSHOT entry: the locked mutate below only
                    # prunes a name whose in-lock entry still equals this one,
                    # so an agent (re)added by a newer sync between this
                    # snapshot and the lock is never deleted on stale evidence.
                    prune_candidates[name] = dataclasses.asdict(agent_cfg)
                    del cfg.agents[name]
                    pruned.append(name)
    except BaseException as exc:
        if not isinstance(exc, Exception):
            raise
        logger.warning("Failed to scan installed agents", exc_info=True)
        try:
            _sel().log_api_access(
                caller=request.get("user", "dashboard"),
                operation="agent.auto_sync",
                outcome="failure",
                source="agent_sync",
            )
        except Exception:
            logger.warning("SEL logging failed for agent sync failure", exc_info=True)
        return web.json_response({"ok": False, "error": "sync failed", "synced": []}, status=500)

    if synced or pruned:
        try:
            # The caller (api_kirocrew_agents_sync) holds _get_config_lock().
            # Persist as a DELTA read-modify-write inside a single sidecar-
            # flock hold: the adds and prunes decided on the snapshot
            # above are re-applied to the document as read inside the lock,
            # so a concurrent writer's unrelated settings are untouchable --
            # a whole-document save() would publish the stale snapshot over
            # them. _drained_to_thread so a cancellation cannot release the
            # asyncio lock while the worker is mid-write.
            to_add = {n: cfg.agents[n] for n in synced if n in cfg.agents}

            def _write_sync() -> tuple[list[str], list[str], list[str]]:
                retired_stores: list[str] = []
                # The names _mutate REALLY deleted -- a subset of the snapshot
                # candidates, because an entry edited between the snapshot and
                # the lock hold survives (see the comment inside _mutate).
                deleted_names: list[str] = []
                # Names this sync could NOT register: their stale team
                # membership could not be purged (see below).
                deferred_names: list[str] = []

                def _mutate(doc: dict) -> dict | None:
                    agents = coerce_dict_section(doc, "agents")
                    stores = coerce_dict_section(doc, "memory_stores")
                    changed = False
                    deferred_names.clear()
                    for aname, acfg in to_add.items():
                        if aname in agents:
                            continue
                        # A discovered name may have been a crew before (its
                        # package was removed and has come back): purge any
                        # stale team membership INSIDE this locked mutation,
                        # right before the name is registered, as every create
                        # path does. The sync is periodic, so a name whose
                        # purge cannot be made is left out of THIS sync and
                        # picked up by the next one -- never registered while
                        # its old membership could resurface.
                        try:
                            teams_mod.release_for_create(aname)
                        except teams_mod.TeamsUnavailable:
                            logger.warning(
                                "sync: deferring agent %r -- its stale team membership "
                                "could not be purged; retrying next sync",
                                aname,
                                exc_info=True,
                            )
                            deferred_names.append(aname)
                            continue
                        agents[aname] = dataclasses.asdict(acfg)
                        changed = True
                    # Prune ONLY this sync's snapshot candidates, and only
                    # while the in-lock entry still equals the snapshot entry:
                    # an agent (re)added or edited between the discovery
                    # snapshot and this lock hold is newer evidence than the
                    # stale discovered_names and must survive.
                    for aname, snap_entry in prune_candidates.items():
                        if agents.get(aname) == snap_entry:
                            store_name = snap_entry.get("memory_store", "")
                            record = stores.get(store_name)
                            if isinstance(record, dict) and record.get("memory_version") == 2:
                                owner = record.get("owner_member")
                                if owner != aname:
                                    raise UnknownMemoryStore(
                                        f"memory store {store_name!r} ownership changed concurrently"
                                    )
                                retired_stores.append(store_name)
                            del agents[aname]
                            deleted_names.append(aname)
                            changed = True
                    return doc if changed else None

                def _drop_pruned() -> None:
                    # A pruned package agent may be on a team; drop it like the
                    # delete route does -- AFTER the registry write committed,
                    # still inside its lock. ONLY the names _mutate actually
                    # deleted: a snapshot candidate that survived (edited
                    # concurrently) keeps its team. Best-effort: the list route
                    # reconciles against the registry anyway.
                    for deleted_name in deleted_names:
                        teams_mod.drop_member(deleted_name)

                with memory_store_namespace_lock():
                    update_config_locked(mutate=_mutate, after_write=_drop_pruned)
                from kiro_crew.context import release_cached_memory_store

                for store_name in retired_stores:
                    release_cached_memory_store(store_name)
                return retired_stores, deleted_names, deferred_names

            retired_stores, deleted_names, deferred_names = await _drained_to_thread(_write_sync)
            # A deferred name is NOT a synced name: it leaves `synced` too, so
            # neither the response nor the audit record reports a registration
            # that did not happen (a refusal is not a commit).
            if deferred_names:
                synced[:] = [n for n in synced if n not in deferred_names]
            if (state := request.app.get("state")) is not None:
                from kiro_crew.dashboard.handlers._shared import release_markdown_memory_store

                for store_name in retired_stores:
                    await release_markdown_memory_store(state, store_name)
        except BaseException as exc:
            if not isinstance(exc, Exception):
                raise
            logger.warning("Failed to save config after agent sync", exc_info=True)
            try:
                _sel().log_api_access(
                    caller=request.get("user", "dashboard"),
                    operation="agent.auto_sync",
                    outcome="failure",
                    source="agent_sync",
                )
            except Exception:
                logger.warning("SEL logging failed for config save failure", exc_info=True)
            return web.json_response(
                {"ok": False, "error": "config save failed", "synced": []}, status=500
            )
        try:
            _sel().log_api_access(
                caller=request.get("user", "dashboard"),
                operation="agent.auto_sync",
                outcome="success",
                source="agent_sync",
                resources=", ".join(synced + [f"-{p}" for p in pruned]),
            )
        except Exception:
            logger.warning("SEL logging failed for agent sync success", exc_info=True)
    else:
        try:
            _sel().log_api_access(
                caller=request.get("user", "dashboard"),
                operation="agent.auto_sync",
                outcome="noop",
                source="agent_sync",
            )
        except Exception:
            logger.warning("SEL logging failed for agent sync noop", exc_info=True)

    if pruned:
        logger.info("Pruned %d stale package agents: %s", len(pruned), ", ".join(pruned))

    return web.json_response({"ok": True, "synced": synced, "pruned": pruned})
