"""The speech recogniser's gateway lifecycle.

The idle sweep, the boot prewarm, the model release at shutdown, and the local
decision model's release.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
import time
from typing import TYPE_CHECKING, Any

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.server import (
        _STT_PREWARM_BOOT_DELAY_SECS,
        _STT_SWEEP_BOOT_DELAY_SECS,
        STT_PROVIDER_LOCAL,
        KiroCrewConfig,
        logger,
    )


def _import_stt_engine() -> Any:
    """Import the recogniser module. BLOCKING: 169 ms cold, numpy plus the binding.

    A named module-level function rather than a closure so the call is observable: the
    invariant a test has to pin is *which thread* this runs on, and there is no other
    seam on an `import` statement.
    """
    from kiro_crew.stt import engine

    return engine


async def _stt_idle_sweep() -> None:
    """Release the resident speech model once it has been idle past its window.

    `WhisperEngine.maybe_evict` also runs on the paths that finish a decode, and that
    call can never fire on its own: it runs microseconds after ``_last_used`` was
    stamped. Idleness is by definition a stretch in which none of those paths run, so
    noticing it needs something that runs anyway.

    Two costs are kept off the gateway's loop, and they are separate problems with
    separate fixes:

    * The boot delay keeps the import out of ``runner.setup()``, which runs before
      either socket binds. Importing there delays the moment the dashboard answers,
      for a janitor whose first useful pass is minutes away.
    * `asyncio.to_thread` keeps the import off the LOOP. Sleeping first moved it out
      of boot but left it running inline on the event loop, where a measured 169 ms
      (numpy plus the recogniser binding) stalls every socket and heartbeat the
      gateway is serving at that moment.
    """
    await asyncio.sleep(_STT_SWEEP_BOOT_DELAY_SECS)
    engine = await asyncio.to_thread(_import_stt_engine)
    await engine.idle_sweep_loop()


def _log_prewarm_outcome(task: "asyncio.Task[None]") -> None:
    """Consume the boot prewarm's result so a failure is logged, not raised.

    The sweep's callback re-raises deliberately: a janitor that died is a defect. This
    one must not, because every reason a prewarm fails (no model on disk, no
    recogniser, a slow load that timed out) is a state the gateway is expected to run
    in, and turning any of them into an unhandled task exception would report a
    working gateway as broken.
    """
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.debug("Boot prewarm of the speech model failed", exc_info=exc)


async def _stt_startup_prewarm() -> None:
    """Load and warm the speech model in the background, shortly after boot.

    **Why this exists.** Prewarming was triggered only by the browser's pointer-down
    on the microphone, which is too late to help: the digest verification and the
    native load sit in front of the first utterance's own decode, so a user who says
    a short phrase and stops is still waiting on them after they have finished
    speaking. Paying them at boot, when nobody is waiting, removes them from the
    first utterance -- and the context is then resident for every later one, so the
    cost lands once per gateway rather than once per cold start.

    **What it does and does not save.** It removes the hash and the load, NOT the
    decode, which has to happen either way. Measured on a 32-core aarch64 CPU build
    (11 s clip, time from "ready to decode" to "transcript in hand"): ``base``
    1.36 s -> 0.66 s, ``small`` 4.39 s -> 2.44 s, ``large-v3-turbo`` 15.47 s ->
    13.59 s. So the saving is 0.7-2.0 s here, and is dominated by the digest check,
    which scales with model size and with how cold the page cache is -- the same
    1.6 GB model hashed in 1.14 s warm and 5.48 s cold, so the upper bound on a cold
    host is several seconds. The first decode's graph allocation, by contrast, is
    negligible on a CPU build: 30-40 ms, measured as the gap between the first and
    second decode after a load.

    **What it deliberately does not do.**

    * It never FETCHES A MODEL THE HOST DOES NOT HAVE. Only an already-present model
      is warmed, checked with ``is_present`` before the engine is asked for anything.
      A gateway that pulls 1.6 GB because it booted would be spending a user's
      bandwidth on a feature they have not used yet, and the first-run download stays
      where it is: an explicit ``POST /api/stt/prepare``.

      Not quite "never downloads", and the gap is worth stating: ``is_present`` is a
      stat, while the load path's ``ensure`` verifies the file against its pinned
      digest. A present-but-corrupt file therefore does re-download here -- a repair,
      not a first fetch, and the alternative would be warming a model whose bytes
      are not the ones we pinned.
    * It never touches the microphone. This is model residency only.
    * It does not block boot, and it does not run on the event loop: the same two
      costs ``_stt_idle_sweep`` documents apply identically here, and the load itself
      goes to the STT executor inside ``prewarm``.
    * It does not fail anything. A missing model, an unavailable recogniser or a
      failed warm decode all leave the gateway exactly as it was -- the next real
      session prepares on its own behalf and reports its own errors.

    Cancelled at shutdown like the sweep. A cancel during the native load cannot stop
    it (there is no abort hook for a load), which is why the work is a plain
    ``await`` on ``prewarm`` rather than something that pretends otherwise: the
    engine's own ``_load_future`` bookkeeping is what keeps a second load from
    starting alongside one that outlived its caller.
    """
    await asyncio.sleep(_STT_PREWARM_BOOT_DELAY_SECS)
    cfg = await asyncio.to_thread(KiroCrewConfig.load)
    if not cfg.stt.enabled or cfg.stt.provider != STT_PROVIDER_LOCAL:
        return
    # Off the loop, and BEFORE the lazy imports below, for the reason
    # `_stt_idle_sweep` documents: this pulls numpy and the recogniser binding,
    # measured at 169 ms, which on the loop stalls every socket the gateway is
    # serving. Both imports below resolve through modules this one has already
    # brought in, so they are cheap by the time they run.
    engine = await asyncio.to_thread(_import_stt_engine)
    from kiro_crew.stt import models as stt_models
    from kiro_crew.transcribe import _whisper_language

    model = stt_models.resolve(cfg.stt.model)
    # `is_present` is a stat, so it runs off-loop with everything else in this step.
    if not await asyncio.to_thread(stt_models.is_present, model):
        logger.debug("Speech model %s is not downloaded; skipping the boot prewarm", model.name)
        return
    # Enough memory to hold it, and enough left over afterwards. The fourth gate,
    # the same shape as the three above: withhold unless we are sure.
    #
    # Without it a boot warm is a guess about intent that costs whatever the chosen
    # model weighs, on every launch, for `idle_evict_secs`. `large-v3-turbo` measured
    # 1861 MB peak RSS on a reviewer's Mac -- and on a desktop install the gateway
    # restarts with the app, so an 8 GB machine pays that per launch whether or not
    # its owner ever dictates that session. The pointer-down prewarm still covers the
    # case, exactly as it did before this task; only the speculative half is skipped.
    #
    # The margin is the model's own size again rather than a tuned constant: the
    # resident cost is roughly the weights plus working buffers, so "twice the
    # weights free" is the cheapest defensible floor, and a reading that could not be
    # taken is treated as "do not speculate".
    #
    # The reading is cgroup-CLAMPED, not the host's `MemAvailable`. In a
    # memory-capped container `/proc/meminfo` reports the host, so a 1.6 GB model can
    # clear a host-wide check and then be OOM-killed against the cgroup limit -- and
    # because this runs on every boot, that is a crash loop no config change escapes.
    # `subagent._available_memory_gb` already takes the minimum of the host reading
    # and the tightest visible cgroup headroom on every platform, so it answers the
    # question this gate is actually asking; `resource_status` reuses it the same way
    # and for the same reason.
    from kiro_crew.subagent import _available_memory_gb

    available_gb = await asyncio.to_thread(_available_memory_gb)
    available_mib = int(available_gb * 1024) if available_gb > 0 else 0
    needed_mib = 2 * model.size_bytes // (1024 * 1024)
    if available_mib <= 0 or available_mib < needed_mib:
        logger.debug(
            "Skipping the boot prewarm for %s: %d MiB available, %d MiB wanted",
            model.name,
            available_mib,
            needed_mib,
        )
        return
    # The engine's bounds come from config here for the same reason the session path
    # passes them: `shared_engine` is a process singleton, and the first caller to
    # supply bounds is the one that sets them. Booting without them would leave the
    # module defaults in force until some later caller happened to pass the
    # operator's real values.
    engine.shared_engine(idle_evict_secs=cfg.stt.idle_evict_secs, timeout_secs=cfg.stt.timeout_secs)
    from kiro_crew import stt

    started = time.monotonic()
    # The package-level `prewarm`, which is the same entry point
    # `POST /api/stt/prewarm` uses. Reused rather than reimplemented so a boot warm
    # and a pointer-down warm cannot drift apart.
    result = await stt.prewarm(
        model_name=model.name,
        language=_whisper_language(cfg.stt.language_code),
    )
    if not result.ok:
        # Debug, not warning: a gateway whose recogniser is unavailable has nothing to
        # act on here, and the surfaces that DO need to say so (the status endpoint,
        # a real session) report it against a user who is actually asking.
        logger.debug("Boot prewarm of the speech model did not complete: %s", result.detail)
        return
    # Off the loop: `capabilities` reads the build (a native call) behind its
    # preflight gate, which can spawn the probe child if the wheel changed.
    backend = (await asyncio.to_thread(engine.WhisperEngine.capabilities)).backend
    logger.info(
        "Speech model %s warmed in the background %.1fs after boot (backend=%s); "
        "the first dictation skips the cold start",
        model.name,
        time.monotonic() - started,
        backend,
    )


def _register_stt_hooks(app: web.Application) -> None:
    """Register the STT idle sweep, the boot prewarm and the model release, for both
    server modes.

    MUST be called BEFORE ``runner.setup()`` freezes the app's signal lists. Shared by
    ``start_dashboard`` and the headless ``start_api_server`` rather than written out
    in each: the two copies were identical, and an event-loop-blocking import in them
    therefore had to be found and fixed twice.
    """

    async def _stt_startup(app_: web.Application) -> None:
        task = asyncio.create_task(_stt_idle_sweep())
        task.add_done_callback(lambda t: t.result() if not t.cancelled() else None)
        app_["stt_idle_sweep"] = task  # prevent GC
        # A SEPARATE task from the sweep, not a step inside it: the sweep is an
        # infinite loop, so folding the prewarm into it would either delay the first
        # sweep by a model load or delay the prewarm by the sweep interval.
        warm = asyncio.create_task(_stt_startup_prewarm())
        warm.add_done_callback(_log_prewarm_outcome)
        app_["stt_boot_prewarm"] = warm  # prevent GC

    async def _stt_shutdown(app_: web.Application) -> None:
        for key in ("stt_idle_sweep", "stt_boot_prewarm"):
            task = app_.get(key)
            if task is None:
                continue
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        # Gated on the engine module having been imported AT ALL, which is the cheap
        # and exact test for "could a model be resident". `stt.close()` resolves
        # through `stt.session`, which imports numpy at module scope and whose
        # `shared_engine()` CREATES an engine if none exists -- so on a gateway that
        # never transcribed anything, closing pulled the recogniser binding and built
        # a WhisperEngine at shutdown purely to release nothing.
        if "kiro_crew.stt.engine" not in sys.modules:
            return
        from kiro_crew import stt

        await stt.close()

    app.on_startup.append(_stt_startup)
    app.on_cleanup.append(_stt_shutdown)

    # The local decision model the provider names runs for as long as the gateway
    # does; ``_kick_local_decision_model`` starts it post-bind. Its server also exits
    # on its own when this process does (it watches the stdin pipe the runtime
    # holds), so this cleanup is the orderly half only.
    async def _local_decision_model_shutdown(app_: web.Application) -> None:
        if "kiro_crew.decisions.local_runtime" not in sys.modules:
            return
        from kiro_crew.decisions import local_runtime

        await asyncio.to_thread(local_runtime.get_runtime().deactivate, wait=True)

    app.on_cleanup.append(_local_decision_model_shutdown)
