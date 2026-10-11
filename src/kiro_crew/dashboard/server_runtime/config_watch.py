"""The live config watcher's gateway wiring.

The appliers whose holder is the dashboard state, the watcher's cleanup, and its start
once the listener serves.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.server import (
        DashboardState,
        KiroCrewConfig,
        logger,
    )


def _register_config_watch(
    app: web.Application, state: DashboardState, initial: KiroCrewConfig | None
) -> None:
    """Arm the live config watcher for both server modes and register the appliers
    that need ``DashboardState``.

    MUST be called BEFORE ``runner.setup()`` freezes the signal lists, because it
    registers the cleanup hook; the watcher itself is started later by
    ``_kick_config_watch``, strictly after the listener binds. *initial* is the
    config this boot loaded, primed so the first tick reports only what
    changed since boot rather than replaying every leaf. Appliers owned by a
    long-lived object (sessions, subagents, channels transports) register in that
    object's constructor; only the ones whose holder is the dashboard state, or
    that must rebuild agent artifacts, live here.
    """
    from kiro_crew.config import live
    from kiro_crew.config.live import ConfigChange
    from kiro_crew.dashboard.handlers.updates import apply_log_level_from_config

    # Wiring only: optional producer import/construction belongs after bind.
    live.bind("dashboard.dynamic_dashboard_cards", state.set_dynamic_cards_enabled)

    async def _switch_provider(cfg: KiroCrewConfig) -> None:
        # Refresh agent artifacts so the target provider is immediately usable.
        # For claude_code this (re)writes ~/.claude/agents/kirocrew.mcp.json --
        # the MCP registry the claude-agent-acp backend reads at session/new --
        # picking up any servers installed while on kiro. Best-effort: a failure
        # here must not block the provider switch (gateway boot also rebuilds).
        try:
            from kiro_crew.agent import rebuild_agent_config

            await asyncio.to_thread(rebuild_agent_config)
        except Exception:
            logger.warning("Agent config rebuild after provider switch failed", exc_info=True)
        # reload_provider_factory() is ONLY for a provider switch: it clears every
        # session and shuts the providers down, which is correct here and wrong
        # for any default change (those go through refresh_defaults()).
        # Installed from the watcher's CURRENT snapshot, not the change this task
        # was scheduled with: the task runs off the cycle, so a later change can
        # already be in force (refresh_defaults installs owner._cfg), and
        # installing the scheduling-time document would silently revert it. A
        # torn snapshot holds defaults, so that case keeps the scheduled config.
        from kiro_crew.config.resolution import DEGRADED_WHOLE_CONFIG

        snap = live.snapshot()
        if snap is not None:
            degraded = snap.degraded_sections
            if DEGRADED_WHOLE_CONFIG not in degraded and "agent" not in degraded:
                cfg = snap
        await state.sessions.reload_provider_factory(cfg=cfg)
        # Clear model on all slots -- aliases are provider-specific.
        for slot in state._slots.values():
            if slot.model:
                slot.model = ""
                # Deliberate model change: bump the pick generation so the
                # fallback restore probe drops any sticky state instead of
                # restoring a model id from the previous provider.
                slot._model_pick_gen += 1
        state.push_slots_update()
        logger.info(
            "Provider switched to %s -- config rebuilt, factory reloaded, slot models cleared",
            cfg.agent.provider,
        )

    async def _apply_provider(change: ConfigChange) -> None:
        if not change.touched("agent.provider"):
            return
        # The switch runs OFF the watcher's cycle, like a channel reconnect. It
        # clears the session registry and then shuts every retired provider down
        # one at a time, which can outlast the applier bound; a timed-out applier
        # is retried on the next tick, and a retried switch would clear the
        # sessions created with the new provider in between. Scheduled as a
        # tracked task, the applier returns at once and the switch runs exactly
        # once per change.
        task = asyncio.create_task(_switch_provider(change.new), name="provider-switch-applier")
        state._background_tasks.add(task)
        task.add_done_callback(state._background_tasks.discard)

    async def _apply_background_model(change: ConfigChange) -> None:
        # The background role model is baked into the lite / heartbeat kiro specs
        # at agent-build time, so a change must rewrite them to take effect. The
        # subagent role is read live at spawn and needs no rebuild.
        if not change.touched("agent.role_models.background"):
            return
        try:
            from kiro_crew.agent import rebuild_agent_config

            await asyncio.to_thread(rebuild_agent_config)
            logger.info("agent.role_models.background changed -- background agent specs rebuilt")
        except Exception:
            logger.warning("background-model rebuild failed", exc_info=True)

    default_model_failure_notified = False

    async def _apply_default_model(change: ConfigChange) -> None:
        # The default (chat) model is baked into the main ``kirocrew`` spec at
        # agent-build time via ``_refresh_dynamic_fields`` (which reads
        # config.json ``agent.model``). A dashboard/CLI change to that key only
        # rewrites config.json, so without this rebuild the installed
        # ``~/.kiro/agents/kirocrew.json`` keeps its previous ``model`` and
        # kiro-cli's ``--agent`` startup loads the STALE pin — a newly created
        # session then runs the old model even though the picker shows the new
        # default (and "auto" can never clear a prior concrete pin). Mirrors
        # ``_apply_background_model``: same authoritative rebuild, keyed on the
        # chat-model config key instead of the background role key.
        if not change.touched("agent.model"):
            return
        nonlocal default_model_failure_notified
        try:
            from kiro_crew.agent import rebuild_agent_config_reporting

            # ``rebuild_agent_config_reporting`` returns ``wrote=False`` — WITHOUT
            # writing — when the shared-home guard refuses to rewrite this
            # instance's spec or the existing spec could not be read. That is a
            # no-op, not a success: the installed
            # ``kirocrew.json`` keeps its old ``model`` pin, so treating it as
            # applied would clear the failure state, broadcast a refresh, and log
            # "rebuilt" while new sessions still run the previous model. Route a
            # refusal into the failure branch below so it notifies once and defers
            # for the watcher's retry, the same as any other unwritten spec.
            _spec_path, wrote = await asyncio.to_thread(rebuild_agent_config_reporting)
            if not wrote:
                raise RuntimeError(
                    "agent spec rebuild did not write (shared agent home refused, "
                    "or the existing spec could not be read); config saved but "
                    "kirocrew.json still pins the previous model"
                )
            # Close the ordering window against SessionManager's own applier.
            # It subscribes to ``agent.model`` FIRST (its subscription predates
            # this one), so on the same change it runs ``refresh_defaults``
            # before this rebuild — draining the warm pool and re-filling it
            # via ``start_pool(blocking=False)`` from the spec as it stood
            # BEFORE the rebuild. A warm provider spawned in that window still
            # pins the previous model until consumed or TTL-evicted. Now that
            # the spec on disk is correct, re-run the same idempotent refresh so
            # any provider minted in the race is discarded and re-spawned from
            # the rebuilt spec. Best-effort and re-entrant: it never touches a
            # live session, and a manager that is absent (tests) simply has no
            # pool to reconcile.
            sessions = getattr(state, "sessions", None)
            if sessions is not None:
                try:
                    await sessions.refresh_defaults(cfg=change.new)
                except Exception:
                    logger.warning(
                        "default-model warm-pool reconcile after rebuild failed",
                        exc_info=True,
                    )
            # The config value and generated agent spec now agree. Tell every
            # dashboard window to refetch both the config-backed picker and the
            # effective-model endpoints only after that rebuild has completed;
            # otherwise an eager refetch can cache the old spec indefinitely.
            recovered = default_model_failure_notified
            default_model_failure_notified = False
            state.push_refresh("agents")
            if recovered:
                try:
                    state.notify(
                        "agent",
                        "Default model applied",
                        "The saved default model is now active. New sessions will use it.",
                    )
                except Exception:
                    logger.debug("default-model recovery notification failed", exc_info=True)
            logger.info("agent.model changed -- kirocrew agent spec rebuilt")
        except Exception:
            logger.warning("default-model rebuild failed", exc_info=True)
            # The config write is already durable, but the generated spec is
            # still the one new sessions actually consume. Surface that split
            # to the operator instead of silently reporting the saved setting
            # as active. Re-raise so ConfigWatch records this subscriber as
            # stale and retries it on later ticks; a successful retry emits the
            # refresh above and brings every open dashboard back into sync.
            if not default_model_failure_notified:
                default_model_failure_notified = True
                try:
                    state.notify(
                        "agent",
                        "Default model could not be applied",
                        "The setting was saved but new sessions will keep using the "
                        "previous model. Kiro Crew retries automatically; "
                        "check the gateway logs if this persists.",
                    )
                except Exception:
                    logger.debug("default-model failure notification failed", exc_info=True)
            raise

    # The workflow-run ceiling and the channel caps are not registered here:
    # WorkflowService and ChannelManager bind their own setters in their
    # constructors (``live.bind``), the rule for an applier a long-lived object owns.
    subs = [
        live.subscribe("agent.provider", callback=_apply_provider, name="agent.provider"),
        live.subscribe(
            "agent.role_models.background",
            callback=_apply_background_model,
            name="agent.role_models.background",
        ),
        live.subscribe("agent.model", callback=_apply_default_model, name="agent.model"),
        live.subscribe(
            "agent.log_level", callback=apply_log_level_from_config, name="agent.log_level"
        ),
    ]
    # Closures are held strongly by the registry; keep the handles on the app so
    # the registrations are visible (and cancellable) from tests.
    app["config_watch_subscriptions"] = subs

    # The watcher is NOT started from ``on_startup``: aiohttp runs those hooks
    # inside ``runner.setup()``, before the listener binds, and ``start()`` awaits
    # an off-loop fingerprint. ``no-new-work-on-gateway-boot-path`` forbids a new
    # awaited step there, so both entrypoints call ``_kick_config_watch`` strictly
    # after ``_start_site`` returns, like the connections scavenge. Only the
    # cleanup hook is registered here, before ``runner.setup()`` freezes the lists.
    app["config_watch_initial"] = initial

    async def _config_watch_shutdown(app_: web.Application) -> None:
        await live.watch().stop()
        lifecycle = getattr(state, "_dynamic_cards", None)
        if lifecycle is not None:
            # One call rather than reaching for a worker attribute: the producer owns two
            # tasks, the model queue and the number refresher, and both must settle.
            await lifecycle.shutdown()

    app.on_cleanup.append(_config_watch_shutdown)


def _kick_config_watch(app: web.Application, state: DashboardState) -> None:
    """Start the live-config watcher as a tracked background task, post-bind.

    Called by both gateway entrypoints only after ``_start_site`` has returned.
    The watcher primes from the config the gateway booted with, then reloads and
    diffs the file on its first cycle, so an edit made between the boot-time
    load and this point is applied rather than lost.

    The prime is done HERE, synchronously, before the task is scheduled:
    ``create_task`` runs nothing until the caller yields, and
    ``GatewayOrchestrator.run`` reaches ``_start_channel_transports`` a few
    awaits after this returns, so a prime left to ``start()`` leaves
    ``live.snapshot()`` at ``None`` for the first transports -- whose per-turn
    reads then fall back to a disk ``KiroCrewConfig.load()``. ``prime`` is a
    plain attribute store, so nothing here awaits on the boot path;
    ``start(initial=...)`` re-primes the same object with the fingerprint unset,
    so the first cycle still reloads and diffs the file.
    """
    from kiro_crew.config import live

    initial = app.get("config_watch_initial")
    watcher = live.watch()
    if initial is not None and not watcher.started:
        watcher.prime(initial)

    async def _start() -> None:
        try:
            current = live.snapshot() or initial
            if current is not None:
                state.set_dynamic_cards_enabled(current.dashboard.dynamic_dashboard_cards)
        except Exception:  # noqa: BLE001 — optional cards must not disable live configuration
            logger.warning("Automatic dashboard cards failed to start", exc_info=True)
        try:
            await live.watch().start(initial=initial)
        except Exception:  # noqa: BLE001 — a dead watcher must not take the gateway down
            logger.warning("Live config watcher failed to start", exc_info=True)

    task = asyncio.create_task(_start())
    state._background_tasks.add(task)
    task.add_done_callback(state._background_tasks.discard)
