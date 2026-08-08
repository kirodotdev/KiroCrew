"""The one request-free way to stop an app's code.

Two callers need to make an app's code stop running: ``POST /api/apps/{name}/disable``
and revoking that app's third-party execution grant. Before this module the revoke
path carried a hand-maintained copy of the disable handler's sequence, and a copy is
a defect with a delay on it: any step added to the handler later would silently not
run on revoke, recreating the "revoke reported success while the backend kept
executing" bug that shipped in the first draft of the grant feature.

So the sequence lives here once, and it deliberately takes NO aiohttp request. The
disable handler's request-bound tail — notification-channel unregistration, the
builtin module's ``on_disable(app)`` hook, builtin service sync — stays in the
handler: those need ``request.app``, and none of them are what makes third-party
code stop. What does is here, in order:

1. ``on_app_disable`` — Python shutdown hooks, route deregistration, cron cleanup.
2. ``stop_app_backend`` — the backend PROCESS. Skipping this is what let a revoked
   app keep running with its app secret and its routes still proxied.
3. ``deregister_app`` — agents, skills, MCP servers.

Ordering matters: hooks first (the app gets to shut down cleanly), then the process,
then the registrations, so nothing re-registers behind us.

Both blocking steps are offloaded to the subprocess executor — ``stop_app_backend``
signals a process group and waits, ``deregister_app`` walks and rewrites registry
files — because this runs on the gateway's event loop, where a slow filesystem would
stall every other request and the heartbeat.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

# Module level, not deferred inside the function: these are the seams both callers'
# tests patch (`patch("kiro_crew.apps.teardown.on_app_disable")`), and a
# function-local import is invisible to `patch`. Safe to import eagerly because
# nothing in this dependency set imports this module back — only routes.py and the
# security handlers do, and both already depend on all of it.
from kiro_crew.apps.backend import (
    recorded_backend_port,
    stop_app_backend,
    unstopped_backend_port,
)
from kiro_crew.apps.bridges import deregister_app
from kiro_crew.apps.hooks_integration import on_app_disable
from kiro_crew.apps.lifecycle_scripts import run_lifecycle_script
from kiro_crew.executors import subprocess_executor
from kiro_crew.security import redact

logger = logging.getLogger(__name__)


@dataclass
class TeardownResult:
    """Outcome of stopping an app's code.

    ``failures`` is the load-bearing field: a caller that must not claim success
    (revoking trust) checks it, while a caller that proceeds regardless (the
    disable handler, whose contract is "disable proceeds anyway with warnings")
    can surface everything and continue.

    The split exists because ``on_app_disable`` reports cron cleanup as PROSE in a
    single field — ``"removed 3 job(s)"`` on success and ``"failed: cron store busy
    — jobs may still be enabled"`` on failure. Treating both as a warning is how a
    contended cron store could leave an app's scheduled commands armed while the
    revoke endpoint returned 200 and reported the app switched off: the same
    "reported success while third-party code kept running" defect this module was
    extracted to kill, one layer up.
    """

    warnings: list[str]
    failures: list[str]

    @property
    def ok(self) -> bool:
        return not self.failures


async def teardown_app_runtime(
    name: str, record: dict[str, Any], *, withdrawing_trust: bool = False
) -> TeardownResult:
    """Stop *name*'s running code.

    Never raises for a failing step: a teardown that aborts halfway leaves the app
    in a worse state than one that pushes through and reports. Each failure is
    logged, collected into ``failures``, and the next step still runs — stopping the
    backend matters even when a shutdown hook threw.

    ``record`` is the app's installed metadata (``manager.get_app``) and is passed
    straight to the app's own shutdown hooks. Hooks and the backend stop run for
    every app unconditionally; the ONE step that varies is bridge deregistration,
    and ``withdrawing_trust`` is what selects between two genuinely different jobs:

    * ``False`` — an ordinary **disable** (a lifecycle operation). The app's
      ``resources`` field is honored: ``"app"`` means the app registered its own
      agents, skills, crons and MCP servers and owns their lifecycle, so the
      gateway must not delete them. ``deregister_app`` removes by app-name prefix
      without asking who created an entry, so calling it here would destroy
      app-owned state on a routine off-switch.
    * ``True`` — **trust withdrawal** (a security operation: revoke, or the blanket
      falling-edge sweep). Deregistration is unconditional, because ``resources``
      is a field of the app's own ``installed.json`` — writable by any app trusted
      to run code — so honoring it here would hand the app a switch for evading its
      own teardown. The trade is deliberate and only defensible in this direction:
      the operator has withdrawn permission for this app's code to be in the
      runtime at all, so leaving a registered execution surface behind is the worse
      failure, and re-granting trust re-registers it.
    """
    warnings: list[str] = []
    failures: list[str] = []
    loop = asyncio.get_running_loop()

    # Every note is scrubbed HERE, as it is created, rather than by each caller.
    #
    # These strings interpolate app-controlled text: the app's own script output,
    # and exception messages raised out of app-owned cron / bridge / backend
    # teardown, which routinely carry paths and URLs. `handle_disable_app` happens
    # to put teardown notes through its own `_redact_warning` on the way out, but
    # the trust-revocation handler returns `warnings` straight on its 200 and
    # performs no redaction of its own. So a note scrubbed only by the caller was
    # scrubbed on exactly ONE of the two paths — and the unscrubbed one was the
    # security operation. Redacting at the source makes that impossible to get
    # wrong again; re-scrubbing already-clean text on the disable path is a no-op,
    # because the placeholders do not match the patterns that produced them.
    #
    # `security.redact` is the canonical DUAL-pass helper (exfiltration URLs, then
    # credentials). `redact_credentials` alone was the gap: a failing `onDisable`
    # that printed a suspicious URL had it reach the response intact.
    def _warn(msg: str) -> None:
        warnings.append(redact(msg))

    def _fail(msg: str) -> None:
        failures.append(redact(msg))

    # The app's OWN ``setup.onDisable`` script, FIRST — before the Python hooks and
    # before the backend process is stopped, because the script may need its own
    # backend alive to shut down cleanly.
    #
    # This step used to live only in the disable HANDLER, which made trust
    # revocation strictly WEAKER than an ordinary off-switch: `onEnable` can start
    # something the gateway never tracked (a detached helper, a daemon it spawned),
    # and `onDisable` is the only thing that knows how to stop it. Revoking trust
    # stopped the tracked backend and hooks, returned 200, and left that helper
    # running — third-party code still executing after its permission to execute was
    # withdrawn. An inversion, since revoke is the security operation and disable is
    # merely lifecycle. Moving it into the ONE shared teardown is what the disable
    # handler's own comment already asked for: a second copy is how the revoke path
    # came to miss steps in the first place.
    #
    # Classified as a WARNING, never a failure, for both callers — the same call as
    # ``hooks_shutdown`` below and for a sharper reason: this script is the app's own
    # code, so treating its failure as fatal would let any app block the withdrawal
    # of its own trust by exiting non-zero, or stall it by hanging. The manifest's
    # ``onDisableTimeout`` bounds the hang, and the tracked teardown below runs
    # regardless, so the app still ends up stopped as far as the gateway can reach.
    setup = (record.get("manifest") or {}).get("setup") or {}
    on_disable = setup.get("onDisable") or ""

    # WHETHER to run the app's own code at all — the one place a persisted flag is
    # consulted, and only ever to run LESS of it.
    #
    # `onDisable` and the `on_shutdown` hook are third-party code. Running them on an
    # app that is not running turns the withdrawal of a permission into a way to
    # EXERCISE it: at this point the grant is still in place (the config write happens
    # after teardown), so the execution gate would admit a shutdown script that only
    # ever runs because someone revoked trust. A disabled app with a crafted
    # `onDisable` gets executed by the very operation meant to stop it.
    #
    # This does NOT contradict the rule that teardown must never gate on the
    # persisted `enabled` flag — that rule exists because the flag can be stale-false
    # while code is still running, so believing it would SKIP work that stops
    # something. Every stopping step below is still unconditional. The flag is used
    # here in the opposite direction, where being wrong is safe: a stale-false flag
    # means we decline to launch a script, not that we leave something running. And
    # it cannot suppress on its own — an OBSERVED backend port overrides it upward,
    # so an app that is actually live still gets its shutdown path even if its
    # metadata claims otherwise.
    #
    # `withdrawing_trust` overrides EVERYTHING here, and that resolves a genuine
    # tension between two opposite review findings about this exact line:
    #
    #   (a) "revoking a disabled app LAUNCHES its code" — the security operation
    #       becoming a way to execute the app.
    #   (b) "revocation can leave DETACHED app code running" — `kirocrew app
    #       disable` is metadata-only and cross-process, so `enabled` can read
    #       false while a helper the app detached is still alive; that helper is
    #       not the tracked backend, so no port is observed either, and the only
    #       thing that knows how to stop it is the app's own `onDisable`.
    #
    # Both are real, and they cannot both be honoured on the revoke path. (b) wins:
    # leaving third-party code running after the operator revoked its permission
    # defeats the entire point of the operation, whereas (a) is not a privilege
    # escalation — the app still HOLDS the grant at this moment (the config write
    # happens after teardown), so running its own documented shutdown hook uses a
    # permission it already has, for the sole purpose of giving it up. It gains
    # nothing it did not have, and `onDisableTimeout` bounds the cost.
    #
    # (a)'s benefit is kept where it is free: on an ORDINARY disable there is no
    # security urgency, so an app that is off and has no observed port still does
    # not get its code launched.
    live_port = await loop.run_in_executor(
        subprocess_executor(), recorded_backend_port, name
    )
    app_may_be_running = (
        withdrawing_trust or record.get("enabled") is True or live_port is not None
    )
    if not app_may_be_running:
        logger.info(
            "skipping %r's own shutdown code: not enabled and no backend port observed",
            name,
        )

    if on_disable and app_may_be_running:
        try:
            script_output = await run_lifecycle_script(
                name,
                on_disable,
                timeout=int(setup.get("onDisableTimeout", 30)),
                action="on_disable",
            )
            if script_output.get("failed"):
                raw = str(script_output.get("output", ""))[:200]
                _warn(f"onDisable script failed: {raw}")
                logger.warning("onDisable failed for %s, continuing teardown", name)
        except Exception as exc:  # noqa: BLE001 - never abort a teardown on the app's script
            _warn(f"onDisable script could not be run: {exc}")
            logger.warning("onDisable could not be run for %s", name, exc_info=True)

    try:
        hooks_result = await on_app_disable(
            name, record, run_app_hooks=app_may_be_running
        )
        # Two outcome fields, deliberately classified DIFFERENTLY, because they say
        # different things about the postcondition this teardown exists to reach:
        #
        # ``cron_cleanup`` failing means the app's scheduled jobs MAY STILL FIRE —
        # third-party code can still execute — so the postcondition is not met and
        # this is a FAILURE. The caller leaves the grant in place and the client
        # retries. The "failed:" marker is the contract in hooks_integration.py.
        #
        # ``hooks_shutdown`` failing means the app's OWN ``on_shutdown`` hook did not
        # succeed, so anything it was buffering may be lost. That is data loss, not
        # continued execution: the backend stop and deregistration below still run,
        # so the app's code still ends up stopped. Blocking the revocation on it
        # would make trust UNREVOKABLE for any app whose cleanup hook is simply
        # broken — refusing to withdraw a permission is worse than the state the app
        # failed to flush — so it is a WARNING the caller surfaces rather than a
        # failure that refuses. Silence was the actual bug: the loop only inspected
        # ``cron_cleanup``, so a failed shutdown hook was neither reported nor acted
        # on and the operator had no way to learn state was dropped.
        if hooks_result:
            for key, value in hooks_result.items():
                if key == "cron_cleanup" and isinstance(value, str) and value:
                    if value.startswith("failed:"):
                        _fail(f"cron cleanup incomplete: {value}")
                    else:
                        _warn(value)
                elif key == "hooks_shutdown" and value == "failed":
                    logger.warning("on_shutdown hook failed for app %r", name)
                    _warn(
                        "the app's own on_shutdown hook failed, so anything it had "
                        "buffered may not have been saved — its code was still stopped"
                    )
    except Exception as exc:  # noqa: BLE001 - a failed hook must not skip the rest
        logger.warning("shutdown hooks failed for app %r: %s", name, exc, exc_info=True)
        _fail(f"hooks disable failed: {exc}")

    # The backend process is stopped for EVERY app, self-managed included: it is the
    # thing actually executing third-party code.
    #
    # The RETURN VALUE is not sufficient on its own, and neither is its absence.
    # `stop_app_backend` answers `False` for two opposite situations — nothing to
    # stop (never started, already dead), and something running it did not stop (a
    # fixed-port backend the gateway never adopted at boot, or an adoption with no
    # usable PIDs). Only the second is a failure, and the flag cannot tell them
    # apart, so the port is OBSERVED instead. That asymmetry is deliberate:
    # reporting a failure whenever the flag is false would make trust UNREVOKABLE
    # for any enabled app whose backend had merely crashed, and refusing to
    # withdraw a permission is worse than the window it would close.
    #
    # The probe runs after EVERY attempt, success included. A `True` return only
    # says "the process I was tracking is gone" — it says nothing about a detached
    # worker the app spawned itself, which keeps the declared fixed port and keeps
    # executing. Gating the observation on the flag was the same mistake in
    # miniature that this comment argues against one paragraph up: it trusted a
    # claim about the runtime instead of looking at it.
    try:
        # Captured BEFORE the stop: `stop_app_backend` drops both the live tracking
        # entry and the pidfile record, and those are the only gateway-owned
        # evidence of which port this backend actually used.
        port_hint = await loop.run_in_executor(
            subprocess_executor(), recorded_backend_port, name
        )
        await loop.run_in_executor(subprocess_executor(), stop_app_backend, name)
        live_port = await loop.run_in_executor(
            subprocess_executor(), lambda: unstopped_backend_port(name, port_hint=port_hint)
        )
        if live_port is not None:
            logger.warning(
                "backend for app %r is still listening on port %s after stop",
                name, live_port,
            )
            _fail(
                f"backend still running on port {live_port} — the gateway stopped "
                "every process it was tracking, so this one is not ours to stop"
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("stopping backend failed for app %r: %s", name, exc, exc_info=True)
        _fail(f"backend stop failed: {exc}")

    # Deregistration is the one step that varies — see the docstring. On an ordinary
    # disable the app's `resources` contract is honored; when trust is being
    # withdrawn it is ignored, because that field is app-written and would otherwise
    # let a trusted app turn off its own teardown.
    if withdrawing_trust or record.get("resources", "gateway") == "gateway":
        try:
            # `deregister_app` reports most problems SOFTLY: it catches internally
            # and returns them on `RegistrationResult.errors` rather than raising.
            # Discarding that return made a registry write failure look like a clean
            # teardown, so revoke would drop the grant while the app's agents,
            # skills, crons or MCP servers were still registered — trust removed on
            # paper, stale execution surface left behind.
            dereg = await loop.run_in_executor(subprocess_executor(), deregister_app, name)
            for err in getattr(dereg, "errors", None) or ():
                logger.warning("deregistering app %r reported: %s", name, err)
                _fail(f"deregister failed: {err}")
        except Exception as exc:  # noqa: BLE001
            logger.warning("deregistering app %r failed: %s", name, exc, exc_info=True)
            _fail(f"deregister failed: {exc}")

    return TeardownResult(warnings=warnings, failures=failures)
