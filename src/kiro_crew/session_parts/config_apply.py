"""How configuration reaches a live ``SessionManager``.

A ``config.json`` write the watcher observes, a compaction threshold another writer
published, and the cold-start queue widths the gateway sizes from the host after it
binds.

Each function here whose first parameter is ``self`` is a ``SessionManager``
member: the class binds it by name (``kiro_crew.session``), so it reads and writes the
manager's own state on the instance, and a patch of the member on the class or
on an instance reaches every caller.

Instance fields written: ``_adopted_autocompact_pct``, ``_cfg``.

Instance fields read: ``_background_runtime``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from kiro_crew.cold_start_sizing import cached_cold_start_concurrency, configure_runtime_spawn_width
from kiro_crew.config import live
from kiro_crew.config.loader import published_autocompact_pct
from kiro_crew.session_allocation import widen_cold_start_semaphore

if TYPE_CHECKING:
    from kiro_crew.config import KiroCrewConfig
    from kiro_crew.config.live import ConfigChange
    from kiro_crew.session import SessionManager


# Paths whose new value is only honoured by a rebuilt provider factory or a
# re-derived warm pool. Anything else under ``session``/``agent`` is read
# off ``_cfg`` (or fresh from the loader) at its point of use, so adopting
# the new config object is the whole apply.
# The sections this manager subscribes to; a degraded one defers the apply.
_CONFIG_SECTIONS: tuple[str, ...] = (
    "session",
    "agent",
    "watchdog",
    "agents",
    "workspaces",
    "default_workspace",
    # aws.project_profiles is read off ``_cfg`` at each spawn.
    "aws",
    # apps.<name>.model is a tier of the new-session model ladder, read off
    # ``_cfg`` at each cold start (session._session_model).
    "apps",
)
_FACTORY_CONFIG_PATHS: tuple[str, ...] = (
    "agent.model",
    "agent.reasoning_effort",
    "agent.acp_backend",
    "agent.role_efforts",
    "agent.tool_search",
    "agent.tool_search_min_pct",
    "agent.tool_search_min_tokens",
    "agent.sandbox",
    "agent.sandbox_allow_no_isolation",
    "agent.sandbox_allow_unsandboxed_exec",
    "agent.member_acp_backend",
    # A warm process carries the environment it was spawned with.
    "agent.env",
    # The warm pool's agent is `session.pool_agent or agent.default_agent`, and
    # WarmPoolState.agent is captured once, so the default is a factory input.
    "agent.default_agent",
    "session.pool_size",
    "session.pool_agent",
    "session.pool_ttl_secs",
    # The warm pool's cwd is `default_project_dir()`, resolved from these two
    # and captured into WarmPoolState, so a workspace edit must re-derive the
    # pool or a cwd-less subagent keeps inheriting the previous directory.
    "workspaces",
    "default_workspace",
)


async def _on_config_change(self: SessionManager, change: ConfigChange) -> None:
    """Hot-apply a ``config.json`` write observed by the config watcher.

    Every writer -- dashboard, ``kirocrew config set``, ``$EDITOR`` -- lands
    here, so nothing below depends on which one wrote. The new config is
    adopted as ``_cfg`` unconditionally: the cleanup loop re-reads its idle
    timeout and RSS ceiling from it every tick, and the soft-stop budget is
    read from it per stop. A change to a factory-bound default goes through
    ``refresh_defaults`` so live sessions keep running while new ones spawn
    on the new defaults. A ``watchdog.*`` or per-crew ``watchdog_*`` change
    re-clamps the windows on every live handle.

    Fails closed like the owned appliers: while any watched section is
    degraded the document holds DEFAULTS for it, and adopting those would
    rebuild the provider factory on the default model and backend and drain
    the warm pool. The manager keeps what is in force and the watcher retries
    each tick until the document validates.
    """
    if change.new.degraded_sections.intersection(self._CONFIG_SECTIONS):
        raise live.ConfigDeferred(change.changed)
    if change.touched(*self._FACTORY_CONFIG_PATHS):
        await self.refresh_defaults(cfg=change.new)
        if change.touched("agent.env"):
            # The shared background runtime is a long-lived process with the
            # environment it was spawned with; the factory rebuild above does
            # not reach it, so free its slot for one that reads the new map.
            await self._background_runtime.retire_for_agent_env_change()
    else:
        async with self._lock:
            self._cfg = change.new
    if change.touched("watchdog", "agent.chat_turn_timeout_secs") or any(
        path.rsplit(".", 1)[-1].startswith("watchdog_") for path in change.under("agents")
    ):
        await self._rebind_live_watchdogs(change.new)


async def refresh_defaults(self: SessionManager, cfg: KiroCrewConfig | None = None) -> None:
    """Adopt changed defaults for new sessions without touching live sessions.

    ``cfg`` is an already-loaded config (the watcher's); ``None`` loads one
    off-loop.
    """
    await self._lifecycle_boundary().refresh_defaults(cfg)


def _sync_autocompact_pct(self: SessionManager) -> None:
    """Adopt a newly published compaction threshold, if one arrived.

    The threshold is captured on ``_cfg`` when the gateway starts, so on its
    own a config write would reach disk and stop there. Every successful
    ``KiroCrewConfig.load`` publishes it, and prompt assembly loads
    config once per turn, so a write from ANY writer -- the dashboard PATCH
    handler or ``kirocrew config set`` -- is in force by the next context
    reading without a restart.

    Adopt-on-CHANGE rather than unconditional assignment: a manager
    constructed with a config that carries its own threshold must keep it, so
    only a value that differs from the last one this manager adopted wins.
    That also makes the sync idempotent, which matters because the gate calls
    it on every reading.

    Reads a module-level snapshot, never config.json -- this runs on the
    event loop, where a stat/read/validate is exactly what the publish idiom
    exists to avoid.
    """
    published = published_autocompact_pct()
    if published != self._adopted_autocompact_pct:
        self._adopted_autocompact_pct = published
        self._cfg.session.autocompact_pct = published


async def reload_provider_factory(self: SessionManager, cfg: KiroCrewConfig | None = None) -> None:
    """Rebuild the provider factory and retire sessions created by the old one.

    ``cfg`` is the config the live applier already holds; ``None`` loads.
    """
    await self._lifecycle_boundary().reload_provider_factory(cfg=cfg)


def widen_cold_start_queues(self: SessionManager) -> None:
    """Grow both cold-start queues to the widths the cached host reading gives.

    The gateway calls this on the loop after its post-bind sizing task has
    read the host, so for ``"auto"`` the manager's ``_start_sem`` and the
    published runtime spawn width move from their floors to the host-sized
    widths without a probe here. An explicit integer is already at its
    width, so this is a no-op for it. Never shrinks a queue.
    """
    agent = getattr(self._cfg, "agent", None)
    configure_runtime_spawn_width(agent)
    widen_cold_start_semaphore(self._start_sem, cached_cold_start_concurrency(agent))
