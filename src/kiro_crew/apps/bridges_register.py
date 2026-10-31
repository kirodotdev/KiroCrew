"""Registering and deregistering an app's resources as a whole.

``register_app`` / ``deregister_app`` and their :class:`RegistrationResult`, refreshing
just an app's agents, pruning resources a manifest upgrade dropped, the startup
reconcile of every enabled app, and dropping the session resume pointers an app's
conversations held (``discard_app_session_pointers``).

Part of the ``kiro_crew.apps.bridges`` family: callers and tests reach every name here
as ``kiro_crew.apps.bridges.<name>``, and a patch there reaches this module too
(:mod:`kiro_crew._facade_mirror`). This module never imports the facade.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from kiro_crew.apps.bridges_agents import (
    _deregister_agents,
    _owned_agents_dir,
    _register_agents,
)
from kiro_crew.apps.bridges_crons import _deregister_crons, _register_crons
from kiro_crew.apps.bridges_mcp import (
    _deregister_mcp_servers,
    _register_mcp_servers,
    _scrub_legacy_shared_mcp,
)
from kiro_crew.apps.bridges_mcp_config import (
    _mcp_lock,
    _read_mcp_json_unlocked,
    _write_mcp_json_unlocked,
)
from kiro_crew.apps.bridges_skills import _deregister_skills, _register_skills
from kiro_crew.apps.bridges_sources import (
    _app_resource_root,
    _kiro_agents_dir,
    _namespace,
    _registration_denied,
    _registration_source,
    _safe_link_name,
)
from kiro_crew.apps.manager import get_app, get_app_manifest, list_apps
from kiro_crew.apps.manifest import (
    AppManifest,
    Containment,
    contained_in_screened_root,
    screen_app_path,
)
from kiro_crew.config.loader import config_dir
from kiro_crew.gateway_lock import GatewayLock, GatewayLockError, LockFileError
from kiro_crew.history import ConversationLog
from kiro_crew.session_map import SUPPRESS_REPLAY_FLAG, SessionMap

logger = logging.getLogger("kiro_crew.apps.bridges")


# ---------------------------------------------------------------------------
# Top-level register / deregister
# ---------------------------------------------------------------------------


@dataclass
class RegistrationResult:
    """Summary of what was registered/deregistered for an app."""

    agents: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    crons: list[str] = field(default_factory=list)
    mcp_servers: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "agents": self.agents,
            "skills": self.skills,
            "crons": self.crons,
            "mcp_servers": self.mcp_servers,
            "errors": self.errors,
        }


def _prune_stale_app_resources(app_name: str, manifest: AppManifest, app_root: Path) -> None:
    """Remove app-owned agents/MCP servers a manifest UPGRADE dropped.

    ``register_app`` OVERWRITES the resources the CURRENT manifest declares, but
    it never deletes ones a new app version removed — so a dropped agent or MCP
    server would stay registered on disk and callable after an upgrade. Prune
    exactly the app-namespaced resources whose names are absent from the current
    manifest, leaving still-declared ones in place (a SELECTIVE prune, not a
    deregister-then-readd that would transiently break a live app). Idempotent: a
    fresh install has nothing to prune.
    """
    # Agents: the current link names the manifest still declares (same derivation
    # as _register_agents: the JSON's `name`, falling back to the file stem).
    current_links: set[str] = set()
    current_agent_names: set[str] = set()
    screened_root = screen_app_path(app_root) if manifest.agents else Containment.UNCHECKED
    for agent_path_str in manifest.agents or []:
        agent_path = app_root / agent_path_str
        try:
            contained = contained_in_screened_root(agent_path, screened_root)
            if contained is Containment.UNCHECKED:
                # An unverified path is not a removed agent: abort like an unreadable one.
                raise OSError("containment could not be verified")
            if contained is not Containment.INSIDE:
                # Outside the root or refused by the path screen: registration
                # never links it, so it carries no current name either.
                continue
            data = json.loads(agent_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            # A declared agent we CANNOT read is not the same as a removed one. If
            # we skipped it, its name would be absent from `current_links` and the
            # prune below would delete its last-good materialized config — over a
            # transient IO error. Abort the AGENT prune entirely (the caller then
            # re-registers current agents idempotently); the MCP prune below still
            # runs off the manifest, which we CAN read.
            logger.warning(
                "Skipping agent prune for %s: declared agent %s is unreadable (%s)",
                app_name,
                agent_path_str,
                exc,
            )
            current_links = None  # type: ignore[assignment]  # sentinel: do not prune agents
            break
        if not isinstance(data, dict):
            # Valid JSON that is not an object (a list, a scalar, null) parses
            # fine, but it carries no readable `name` — the same cannot-read !=
            # removed situation as above. Abort the agent prune so this agent
            # does not fall out of `current_links` and lose its last-good
            # materialized config.
            logger.warning(
                "Skipping agent prune for %s: declared agent %s is not a JSON object",
                app_name,
                agent_path_str,
            )
            current_links = None  # type: ignore[assignment]  # sentinel: do not prune agents
            break
        agent_name = data.get("name", agent_path.stem)
        current_links.add(_safe_link_name(_namespace(app_name, agent_name)) + ".json")
        current_agent_names.add(str(agent_name))
    agents_dir = _kiro_agents_dir()
    if current_links is not None and agents_dir.is_dir():
        # A dropped agent's launch copy (apps.agent_launch) carries its grants, and
        # kiro-cli would keep loading it as a mode, so it goes with the spec.
        from kiro_crew.apps.agent_launch import remove_app_agent_launches

        prefix = _safe_link_name(app_name + "/")
        with _owned_agents_dir(agents_dir, app_name):
            for entry in agents_dir.iterdir():
                if (
                    entry.name.startswith(prefix)
                    and entry.name.endswith(".json")
                    and entry.name not in current_links
                ):
                    try:
                        entry.unlink()
                        logger.info("Pruned stale app agent %s (absent from manifest)", entry.name)
                    except OSError:
                        pass
            remove_app_agent_launches(app_name, keep_agents=current_agent_names)

    # MCP servers: keep only servers the current manifest still declares.
    current_servers = {f"{app_name}:{srv}" for srv in (manifest.mcpServers or {})}
    with _mcp_lock():
        data = _read_mcp_json_unlocked(strict=True)
        servers = data.get("mcpServers", {})
        if isinstance(servers, dict):
            stale = [
                k for k in servers if k.startswith(f"{app_name}:") and k not in current_servers
            ]
            for k in stale:
                del servers[k]
            if stale and _write_mcp_json_unlocked(data):
                logger.info("Pruned %d stale app MCP server(s) for %s", len(stale), app_name)


def register_app(app_name: str) -> RegistrationResult:
    """Register all executable resources for an admitted installed app.

    Third-party resources come from the installed app snapshot. Shipped builtin
    resources come from the immutable manifest root that proves their provenance.

    Apps with ``resources="app"`` manage their own resource registration
    (agents, skills, MCP servers via SDK). Bridge registration is skipped
    entirely to avoid creating duplicates that confuse kiro-cli.
    """
    result = RegistrationResult()
    if not get_app_manifest(app_name):
        result.errors.append(f"app {app_name!r} not found or has invalid manifest")
        return result

    # Self-managed apps handle their own registration — skip all bridge work.
    info = get_app(app_name)
    if info and info.get("resources") == "app":
        logger.debug(
            "Skipping bridge registration for %s (resources=app)",
            app_name,
        )
        return result

    manifest, app_root = _registration_source(app_name)
    if manifest is None:
        result.errors.append(f"app {app_name!r} has no authoritative resource manifest")
        return result

    denied = _registration_denied(
        app_name,
        action="resource_register",
        app_root=app_root,
    )
    if denied:
        # Re-registration can happen after an operator tightens policy. Scrub
        # derivative links/config from a prior admitted run as well as skipping
        # every new side effect. Running cron jobs are removed by the scheduler
        # reconciliation boundary, which owns the CronService instance.
        cleanup = deregister_app(app_name)
        result.errors.extend(cleanup.errors)
        result.errors.append(f"registration blocked by execution policy: {denied}")
        return result

    # NOTE: pruning of resources a manifest UPGRADE removed is NOT done here.
    # The enable/update route handlers all deregister_app() first, so nothing
    # stale survives for them to prune — a prune here (a directory walk over
    # every agent file + lock acquisition, scaling with agent count) would run
    # redundantly on every one of those deregister-first calls. The one path
    # that re-registers WITHOUT a preceding deregister — the boot reconcile —
    # does the prune itself, off the loop, in
    # reconcile_enabled_app_resources(). See that function.

    # MCP servers BEFORE agents: _register_agents copies the app's own registered
    # server specs into each agent config, so registering them afterwards leaves
    # every agent holding the previous spec (or none on a first install) until
    # something else happens to rewrite it. The ordering is the whole fix — both
    # steps individually looked correct.
    try:
        result.mcp_servers = _register_mcp_servers(app_name, manifest)
    except Exception as exc:
        result.errors.append(f"MCP server registration failed: {exc}")

    try:
        result.agents = _register_agents(app_name, manifest, app_root)
    except Exception as exc:
        result.errors.append(f"agent registration failed: {exc}")

    declared = len(manifest.agents or [])
    if declared and not result.agents:
        result.errors.append(
            f"registered 0 of {declared} declared agent(s) for {app_name!r} "
            "-- agent source missing or unreadable"
        )

    unchecked_skills: list[str] = []
    try:
        result.skills = _register_skills(app_name, manifest, app_root, unchecked_skills)
    except Exception as exc:
        result.errors.append(f"skill registration failed: {exc}")
    if unchecked_skills:
        result.errors.append(
            "skill path(s) could not be checked yet, retry registration: "
            + ", ".join(unchecked_skills)
        )

    try:
        result.crons = _register_crons(app_name, manifest)
    except Exception as exc:
        result.errors.append(f"cron registration failed: {exc}")

    logger.info(
        "Registered app %s: %d agents, %d skills, %d crons, %d mcp, %d errors",
        app_name,
        len(result.agents),
        len(result.skills),
        len(result.crons),
        len(result.mcp_servers),
        len(result.errors),
    )
    return result


def refresh_app_agents(app_name: str, io_failures: list[str] | None = None) -> list[str]:
    """Re-materialize just this app's agent configs.

    Called when something the agent config is derived from changes (the app's MCP reach
    policy, or a health scrub removing a server the agents copied) so the change takes
    effect without a gateway restart. Cheap and idempotent — it rewrites the same files
    registration would.

    ``io_failures``, when supplied, collects the agents skipped for an OS-level read or
    write error, so a caller that RETRIES can tell a transient failure from the several
    permanent reasons this returns an empty list — a self-managed app, a denied one, or a
    manifest declaring no agents are all "nothing for us to do", not failures.
    """
    manifest = get_app_manifest(app_name)
    if not manifest or not manifest.agents:
        return []
    info = get_app(app_name)
    if info and info.get("resources") == "app":
        return []
    app_root = _app_resource_root(app_name)
    # Admission gate — mirror register_app. A re-materialization MUST honor the
    # same execution decision, or a revoked app's agents (and the MCP servers
    # merged into them) would be rewritten and become dispatchable again. On
    # denial, scrub any stale materialized agents and register nothing.
    if _registration_denied(app_name, action="resource_register", app_root=app_root):
        from kiro_crew.agent import SharedAgentHomeRefused

        try:
            _deregister_agents(app_name)
        except SharedAgentHomeRefused as exc:
            logger.error("App %s is denied but its agents were not scrubbed: %s", app_name, exc)
        return []
    return _register_agents(app_name, manifest, app_root, io_failures=io_failures)


def reconcile_enabled_app_resources() -> dict[str, int]:
    """Re-register resources for every ENABLED gateway-managed app.

    Called once at gateway startup.  Registering ONLY in the enable path would
    leave an app that gains agents/skills in a later version unregistered for a
    user who has already enabled it — silently, because a missing resource only
    logs a warning.  Reconciling at boot makes the on-disk state a function of
    the current manifests rather than of install history.

    Idempotent: agent configs are rewritten from their template, skills/crons/MCP
    registration already overwrite in place.  Apps with ``resources="app"`` are
    skipped by :func:`register_app` itself.
    """
    counts = {"apps": 0, "agents": 0, "skills": 0, "errors": 0}
    try:
        apps = list_apps()
    except Exception as exc:  # noqa: BLE001 — never block startup
        logger.warning("Resource reconcile skipped: cannot list apps: %s", exc)
        return counts

    for info in apps:
        if not info.get("enabled"):
            continue
        name = info.get("name") or ""
        if not name:
            continue
        # Prune stale resources a manifest UPGRADE removed, BEFORE re-registering.
        # This runs ONLY here, on the off-loop boot reconcile: register_app no
        # longer prunes (it is also called on the event loop, where the directory
        # walk + lock would stall chat/heartbeat), and this is the one path that
        # re-registers without a preceding deregister_app(), so a dropped agent or
        # MCP server would otherwise stay registered and callable.
        try:
            _man, _root = _registration_source(name)
            if _man is not None:
                _prune_stale_app_resources(name, _man, _root)
        except Exception as exc:  # noqa: BLE001 — a prune failure must not block reconcile
            logger.warning("Could not prune stale resources for %s: %s", name, exc)
        try:
            result = register_app(name)
        except Exception as exc:  # noqa: BLE001 — one bad app must not stop the rest
            logger.warning("Resource reconcile failed for %s: %s", name, exc)
            counts["errors"] += 1
            continue
        counts["apps"] += 1
        counts["agents"] += len(result.agents)
        counts["skills"] += len(result.skills)
        counts["errors"] += len(result.errors)
        # Finish the shared-file migration for apps that are ALREADY enabled.
        # Registration now targets the agent config, but an older build wrote
        # this app's servers into the shared ``~/.kiro/settings/mcp.json``, and
        # re-registering does not remove what is already there. Scrubbing only
        # on deregister meant the leak this change exists to fix (one app's
        # private tools visible to Kiro IDE and every other kiro-cli agent, plus
        # dead-port entries that break unrelated sessions) survived until the
        # user happened to disable the app. Best-effort and idempotent.
        scrubbed = _scrub_legacy_shared_mcp(name)
        if scrubbed:
            logger.info(
                "Removed %d stale %s entr%s from the shared kiro MCP config",
                scrubbed,
                name,
                "y" if scrubbed == 1 else "ies",
            )
        for err in result.errors:
            logger.warning("Resource reconcile issue for %s: %s", name, err)

    if counts["apps"]:
        logger.info(
            "Reconciled resources for %d enabled app(s): %d agent(s), %d skill(s), %d error(s)",
            counts["apps"],
            counts["agents"],
            counts["skills"],
            counts["errors"],
        )
    return counts


def app_conversation_keys(app_name: str, *, mapped_keys: Iterable[str] | None = None) -> list[str]:
    """Session keys that still hold a resume pointer and belong to *app_name*.

    A slot key an app chooses is often DETERMINISTIC — one slot per object it tracks,
    named after that object — so the next slot created under that name is the same
    key, and ``session.py``'s ``resume_sid = self._session_map.get(key)`` hands it the
    previous conversation. That is correct while the app is installed: it is how a
    long-lived per-object conversation keeps what it has learned about that object.
    It stops being correct once the app is gone: reinstall it, open the same object,
    and the first turn resumes a conversation from the previous installation — a
    transcript from code that is gone.

    Ownership is read from the record that already outlives the tab: every save
    writes ``app`` into the conversation's metadata line, and closing a tab is a
    save. Live ``_ChatSlot._app`` cannot answer here (the CLI path has no gateway,
    and a closed slot is gone from a running one), and ``open_slots.json`` cannot
    either — it tracks tabs to REOPEN, so a closed slot leaves it while the resume
    pointer deliberately stays. A closed app slot is the mainline state before an
    uninstall, so sourcing ownership from open tabs would miss the common case.

    Scoped to keys that still have a sid, which makes this index exactly as wide as
    the problem: a pointer exists only if a conversation was started, and starting
    one writes the metadata line this reads.

    **Which keys to look at is the caller's answer, not this function's, whenever
    the caller owns a live map.** A detached ``SessionMap`` reads the file, and the
    file lags a running gateway's map by whatever that map has not flushed, so a
    detached enumeration can omit a key whose pointer already exists — and the
    in-gateway caller then clears through the live map, leaving the pointer it
    could not see behind for a reinstall to resume. So that caller passes
    *mapped_keys* from its own manager (``mapped_session_keys() | session_keys()``,
    the second covering an allocation still in flight), and the detached read is
    reserved for the CLI path, which holds the gateway lock and therefore knows no
    live map exists. Reading detached is sound only because nothing here writes —
    the class's rule 3 forbids a detached WRITE.

    Returns keys sorted for stable logs. Never raises: an uninstall the user asked
    for does not fail over bookkeeping.
    """
    if not app_name:
        return []
    try:
        log = ConversationLog()
        candidates = SessionMap().mapped_sids_by_key() if mapped_keys is None else set(mapped_keys)
        owned: list[str] = []
        unreadable: list[str] = []
        for key in candidates:
            meta, readable = log.get_metadata_status(key)
            if meta.get("app") == app_name:
                owned.append(key)
            elif not readable:
                # A record that could not be READ is not a record that says "not this
                # app", and the two are the same value here — ``{}``. Reported rather
                # than guessed either way: claiming it would sweep up a conversation
                # this app may not own, which is worse than the pointer it leaves, and
                # dropping it in silence hides the one case where the answer is
                # unknown. Not raised, because an uninstall the user asked for does
                # not fail over bookkeeping.
                unreadable.append(key)
        if unreadable:
            logger.warning(
                "could not read the ownership record of %d conversation(s) while "
                "resolving %r's keys, so their resume pointers are left in place: %s",
                len(unreadable),
                app_name,
                ", ".join(sorted(unreadable)[:10]),
            )
        return sorted(owned)
    except Exception:  # noqa: BLE001 — bookkeeping must not fail an uninstall
        logger.warning("resolving %r's conversation keys failed", app_name, exc_info=True)
        return []


@dataclass(frozen=True)
class SessionPointerCleanup:
    """What a CLI-path pointer clear actually did.

    ``dropped == 0`` alone cannot be reported to an operator: it is "this app owned
    nothing", "a gateway owns the map, so nothing was attempted", and "the write
    itself failed", and those need different messages — the first is complete, the
    other two leave a pointer a reinstall will resume. ``declined`` and ``failed``
    are what separate them.

    ``failed`` covers the case the clear could not PERSIST: the sid clear and the
    suppression flag are both mutations of a file that has to be written, so an
    ENOSPC or a permission error leaves the pointer on disk while the count says
    nothing was owned. It also covers a lock that could not be used at all
    (:class:`~kiro_crew.gateway_lock.LockFileError`: ``gateway.lock`` is not a
    regular file, or it or the home could not be opened, created or measured),
    because no gateway owns the map then and stopping one would not help. Reported
    rather than raised — the uninstall itself already
    succeeded, and bookkeeping must not fail it — but not reported as clean either.
    """

    dropped: int = 0
    declined: bool = False
    failed: bool = False


def discard_app_session_pointers(app_name: str) -> SessionPointerCleanup:
    """Drop the resume pointer of every conversation this app owned. CLI PATH ONLY.

    Called by the UNINSTALL paths only, and deliberately NOT from
    :func:`deregister_app`: that runs on **disable** too (the CLI's disable action,
    the disable route, and the enable/update reconcile), and disable is how an app
    is routinely restarted — App Store Sync is a disable/enable pair. Clearing
    pointers there would discard the accumulated context of every long-lived
    conversation the app owns, on every sync.

    ``clear_sid``, not ``delete``: the entry also carries the Slack thread/channel
    linkage and the reverse index built from it, so dropping the whole row would
    silently unlink a mirrored session. The cleared value is stashed as
    ``discarded_sid``, so this is diagnosable and reversible by hand — the native
    conversation itself is untouched on disk.

    **Holds the gateway lock across the whole scan and write, and that is the
    mechanism, not a courtesy.** This writes through a throwaway ``SessionMap``,
    which is only sound when no other instance holds the file: a running gateway
    keeps a long-lived map whose ``_data`` loaded at startup and whose every write
    rewrites the whole file from that snapshot, so two writers do not merge — the
    loser's rows vanish. Writing anyway would both fail to drop the pointer (the
    live map's next write restores it) and drop whatever that map recorded since
    this process read, costing an unrelated session its sid or its channel link.
    Merely *asking* whether a gateway is up cannot establish that: a gateway
    starting between the question and the write lands in exactly the case the
    question was meant to exclude. Taking the same lock the gateway takes makes the
    exclusion real in both directions — while we hold it no gateway can start, and
    if one is already up we cannot take it and decline instead. Doing nothing is
    the pre-existing behaviour; corrupting a stranger's session is not.

    The cost is stated rather than hidden: a gateway attempting to start inside this
    window is refused as it would be by any other holder. The window is one scan
    plus one write, and the alternative is losing another session's row.

    The in-gateway route does not come through here at all — it clears through the
    live map, inside the app lifecycle lock.

    Returns what it did, not just how much: a declined clear leaves a pointer behind
    and the caller has to be able to say so.
    """
    try:
        with GatewayLock(config_dir()):
            keys = app_conversation_keys(app_name)
            if not keys:
                return SessionPointerCleanup()
            smap = SessionMap()
            # ``clear_sid`` reports whether it dropped anything, so the count is
            # conversations orphaned by THIS uninstall rather than keys looked at.
            # The suppression flag goes on every owned key regardless: dropping the
            # sid stops the NATIVE resume only, and the transcript stays on disk, so
            # without it the next installation's first turn under this key gets the
            # removed app's history injected as replay. A key whose pointer was
            # already gone still has a transcript, so it still needs the flag.
            cleared = 0
            for key in keys:
                if smap.clear_sid(key):
                    cleared += 1
                smap.set_flag(key, SUPPRESS_REPLAY_FLAG, True)
            # Durable BEFORE the lock is released. Off-loop writes are inline today,
            # so this is belt and braces — but the ordering is the invariant, not a
            # property inherited from being off the loop, which a later refactor
            # could change without noticing this depends on it.
            smap.flush()
    except LockFileError as exc:
        # Not a gateway owning the map: the lock file itself could not be used
        # (not a regular file, or an open/create/stat failure), and stopping a
        # gateway would not help. ``failed`` sends the operator to the log, where
        # this names the path and the cause.
        logger.warning("session pointer cleanup for %r could not lock: %s", app_name, exc)
        return SessionPointerCleanup(failed=True)
    except GatewayLockError:
        logger.info(
            "Left %s's conversation pointers in place: a gateway owns session_map.json "
            "and a second writer would drop rows it has not flushed yet",
            app_name,
        )
        return SessionPointerCleanup(declined=True)
    except Exception:  # noqa: BLE001 — bookkeeping must not fail an uninstall
        logger.warning("session pointer cleanup for %r failed", app_name, exc_info=True)
        # ``failed``, never the default: the scan and the write are both in this
        # block, so an ENOSPC or permission error on ``flush`` lands here with the
        # pointer still on disk. Returning the default would tell the caller the app
        # owned nothing, and the caller prints a success tick on that.
        return SessionPointerCleanup(failed=True)
    if cleared:
        logger.info(
            "Dropped %d resume pointer(s) for %s so a reinstall starts fresh",
            cleared,
            app_name,
        )
    return SessionPointerCleanup(dropped=cleared)


def deregister_app(app_name: str) -> RegistrationResult:
    """Deregister all resources for an app.

    Removes symlinks and cron manifests.  Does not remove the app directory.

    Does NOT touch session resume pointers — see
    :func:`discard_app_session_pointers` for why that belongs to uninstall alone.
    """
    result = RegistrationResult()

    try:
        n = _deregister_agents(app_name)
        result.agents = [f"removed {n} agent(s)"]
    except Exception as exc:
        result.errors.append(f"agent deregistration failed: {exc}")

    try:
        _deregister_skills(app_name)
        result.skills = ["removed"]
    except Exception as exc:
        result.errors.append(f"skill deregistration failed: {exc}")

    try:
        _deregister_crons(app_name)
        result.crons = ["removed"]
    except Exception as exc:
        result.errors.append(f"cron deregistration failed: {exc}")

    try:
        n = _deregister_mcp_servers(app_name)
        result.mcp_servers = [f"removed {n} MCP server(s)"]
    except Exception as exc:
        result.errors.append(f"MCP server deregistration failed: {exc}")

    # No worker re-derive here, deliberately. This file is one of SIX writers of
    # `kirocrew.json` under two different file locks, so a re-derive per writer leaks
    # one hole per writer nobody named. `agent.require_fresh_derived_spec` refuses a
    # stale mirror on the SPAWN path instead, which covers every writer including the
    # ones this module does not own -- and cannot lose the race a post-write hook can.
    logger.info("Deregistered app %s", app_name)
    return result
