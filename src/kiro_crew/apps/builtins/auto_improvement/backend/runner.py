"""Run supervisor — owns the spine :class:`~..spine.driver.Driver` on a worker thread.

## Why a thread and not a task

The spine is synchronous by design: every measurement is a bounded ``subprocess``
that must be timed with nothing else contending for the host (the A/B discipline is
strictly serial on purpose). Making it async would either be a lie — ``asyncio``
cannot make a blocking ``pytest`` run yield — or would let concurrent work
contaminate the very timings the ruler exists to trust.

So the driver runs on ONE ``threading.Thread`` and the async gateway never touches
it directly. Routes call :meth:`RunSupervisor.start` / :meth:`status` / :meth:`stop`,
which are cheap, lock-guarded, non-blocking operations over in-memory state; the
thread pushes progress INTO that state through the driver's ``on_progress`` sink.
The only synchronization is one :class:`threading.Lock` around a small mutable
:class:`RunState`, held for microseconds and never across a subprocess call.

## Refusals, up front

A run is refused (never half-started) when the clone's push is not mechanically
disabled, when no repository is configured, or when a run is already active. The
push check is the app's #1 safety control and it is re-asserted HERE, before the
thread starts, so a refusal is a synchronous 409 the user sees rather than an
exception buried in a worker thread's traceback. The driver asserts it again at
``run()``; both checks call the same profile predicate, so they cannot disagree.

## Defaults are deliberately small

``max_cycles=3``, a per-run wall-clock cap, a cost cap, and a single wide/deep
proposer fan-out. A first run should finish and produce a legible verdict, not spend
an afternoon and $50. Every one of these is overridable from config.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import clone_setup, store

logger = logging.getLogger(__name__)

#: Activity ring buffer size. Bounded because the driver emits an event per stage per
#: candidate per cycle and this state is polled, never drained — an unbounded list is a
#: slow memory leak for a run left going overnight.
#: Live-activity ring size. 5000, matching upstream, which raised it from exactly this
#: 200 after operators found a multi-cycle run scrolled its early work off-screen — the
#: agent's own reasoning is the primary diagnostic for "why did this candidate die?", and
#: a single propose stage emits hundreds of lines. Observed here too: the wide+deep
#: authoring of one candidate evicted the whole discovery phase. Still bounded (a deque
#: maxlen) so a runaway run cannot grow memory without limit; ~5000 small dicts is a few
#: MB at worst and the status endpoint serializes only what the UI asks for.
ACTIVITY_MAXLEN = 5000

#: Starting budget. Every value is overridable from config.
#:
#: ``max_cycles`` is deliberately GENEROUS relative to ``max_hours``, because the cycle
#: cap is the one that silently starves a run: discovery emits several surfaces per
#: cycle but only ``wide + deep`` of them are ever attempted, so a low cycle cap leaves
#: the rest sitting at ``seen`` forever — they are not rejected, just never tried.
#: Measured on this app's own dogfood: 17 of 31 findings (55%) were never attempted, and
#: the run still stopped with 0.72h of its 1.5h unused. Let TIME (and quiescence) end a
#: run — that is what those bounds are for — rather than an arbitrary cycle count.
DEFAULT_MAX_CYCLES = 25
DEFAULT_MAX_HOURS = 2.0
DEFAULT_MAX_COST_USD = 5.0

#: How long :meth:`RunSupervisor.stop` waits for the driver to finish its current
#: candidate. The spine stops between candidates, not mid-measurement, so a stop lands
#: after at most one gate+measure — bounded, but not instant.
STOP_JOIN_TIMEOUT_S = 30.0

STATUS_IDLE = "idle"
STATUS_RUNNING = "running"
STATUS_CALIBRATING = "calibrating"
STATUS_STOPPING = "stopping"
STATUS_DONE = "done"
STATUS_ERROR = "error"


@dataclass
class RunState:
    """Everything :meth:`RunSupervisor.status` reports. Mutated only under the lock."""

    status: str = STATUS_IDLE
    run_id: str = ""
    cycle: int = 0
    kept: int = 0
    drafted: int = 0
    error: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0
    stage: str = ""
    #: Bounded newest-last event log the UI polls.
    activity: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=ACTIVITY_MAXLEN))
    #: The last ``preflight`` event's measured numbers (band, baseline n, canary delta).
    preflight: dict[str, Any] = field(default_factory=dict)
    #: The last ``budget``/``quiescence`` snapshots, for the dashboard's cards.
    budget: dict[str, Any] = field(default_factory=dict)
    quiescence: dict[str, Any] = field(default_factory=dict)
    #: Terminal run counters, copied off the spine's ``Stats`` when the loop returns.
    stats: dict[str, Any] = field(default_factory=dict)


#: Stands in for one activity string the redactor could not scan. A placeholder rather than a
#: dropped field, so the feed keeps its shape and the operator still sees the run progressing.
_UNSCANNED = "[withheld: redaction unavailable]"


def _credentials_are_unconfined() -> str:
    """A REASON string when a provider-driven agent would run without credential masking.

    Empty string means "confined, safe to run". The app's subprocess path forces
    ``sandboxed_spawn_argv(mode="strict")`` + ``strip_credential_env``; the provider path
    inherits the gateway's ``sandbox`` setting instead. Only ``"cc"`` and ``"strict"``
    profiles hide credential stores (``~/.aws``, ``~/.ssh``, ``~/.config/gh``, ``~/.kube``);
    the default ``"auto"``/``"standard"`` intentionally exposes ``.aws/.ssh`` for
    interactive workflow use — safe for human-driven chat, but NOT for unattended
    repository-controlled execution where a crafted instruction could read credentials.

    FAIL CLOSED on an unreadable config: a state we cannot verify is treated as unconfined,
    because the alternative is running an agent over repository-controlled text with the
    operator's credentials visible.

    The operator can ACKNOWLEDGE the residual risk with ``acceptUnsandboxedAgentRisk``
    (default OFF, compared with ``is True`` so only the explicit boolean opts in) — the same
    one-time-consent shape as the watcher's ``watcherAcceptEgressRisk`` (D-118). That escape
    hatch exists so a hard refusal doesn't silently take the loop offline rather than
    telling the operator what to decide. Raised by the GPT review.
    """
    if _unsandboxed_agent_accepted():
        return ""
    try:
        from kiro_crew.config import KiroCrewConfig

        mode = str(getattr(KiroCrewConfig.load(), "sandbox", "") or "").strip().lower()
    except Exception as exc:  # noqa: BLE001 — an unverifiable sandbox is an unconfined one
        return f"the gateway sandbox setting could not be read ({type(exc).__name__})"
    # The provider path runs repository-controlled text through an agent with
    # auto-approved shell, so it requires a sandbox level that HIDES credential
    # stores (~/.aws, ~/.ssh, ~/.config/gh, ~/.kube). Only 'cc' and 'strict'
    # do this; 'auto'/'standard' intentionally EXPOSE .aws/.ssh for interactive
    # workflow use — safe for human-driven chat, but not for unattended
    # repo-controlled execution.
    _CREDENTIAL_HIDING_MODES = {"cc", "strict"}
    if mode not in _CREDENTIAL_HIDING_MODES:
        return (
            f"the gateway sandbox is {mode or 'unset'!r} — the auto-improvement "
            f"provider path requires 'cc' or 'strict' (credential-hiding profiles) "
            f"or the explicit acceptUnsandboxedAgentRisk opt-in"
        )
    return ""


def _unsandboxed_agent_accepted() -> bool:
    """The ``acceptUnsandboxedAgentRisk`` flag. OFF unless explicitly turned on.

    Fail-closed and compared with ``is True`` (not truthiness) so a stray string or ``1``
    does not opt in — same contract as ``pr_watchers._watcher_egress_accepted``.
    """
    config = store.read_json(store.config_path(), {}) or {}
    return bool(config.get("acceptUnsandboxedAgentRisk") is True)


def _redact_activity(value: Any) -> Any:
    """Redact credentials / exfiltration URLs from anything entering the activity feed.

    The feed is an EGRESS PATH the drift guard cannot see: it walks redactor call sites,
    and this module had none. Every entry is served verbatim by ``RunSupervisor.status()``
    -> ``GET /run`` -> ``activityLine`` in the browser, and the strings inside are raw
    model output — assistant text and the ``command`` of a bash tool call. A credential
    the discovery agent reads out of the target clone and quotes in its turn would cross
    to the dashboard unscanned, which is exactly the class of text
    ``pr_watchers._redact`` and ``routes._redact_for_display`` already scan.

    Recursive because the agent event is a nested dict (``{"agent": {"detail": ...}}``);
    redacting only the top level would miss the field that actually carries the text.

    FAIL-CLOSED. An earlier revision failed OPEN here, reasoning that this feed is the
    operator's only live view and "nothing here leaves the host". The second half was wrong:
    ``status()`` puts this list straight into the ``GET /run`` JSON, so it is served to a
    browser — the same egress boundary ``routes._redact_for_display`` already fails CLOSED on.
    Measured with a raising redactor: ``aws_secret_access_key=…`` reached the response payload
    verbatim. Raised by the GPT review of this branch.

    Failing closed does NOT blank the feed — that was the real concern behind the original
    choice. Each unscannable STRING becomes a fixed placeholder while the event's structure,
    timestamps and every other field survive, so the operator still sees that a run is
    progressing and what kind of step it is on.
    """
    # Function-local ON PURPOSE, not an oversight — review flagged it against
    # `top-level-imports`. The `try/except` around the import IS the fail-closed mechanism: if
    # the redactor cannot be imported, this returns a placeholder and the run keeps going with
    # nothing unscanned served. Hoisted to module scope, the same failure becomes an
    # ImportError at MODULE LOAD, which takes the whole app's backend down instead of
    # degrading one field — strictly worse for an egress guard whose entire job is to never
    # fail open. The rule's own intent (make dependencies visible) is served by this comment.
    try:
        from kiro_crew.security import redact
    except Exception:  # noqa: BLE001 - cannot scan → do not serve unscanned agent text
        return _UNSCANNED if isinstance(value, str) else value
    if isinstance(value, str):
        try:
            return redact(value)
        except Exception:  # noqa: BLE001
            return _UNSCANNED
    if isinstance(value, dict):
        return {k: _redact_activity(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_activity(v) for v in value]
    return value


class RunSupervisor:
    """Owns at most one in-flight spine run and the state the routes report on."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state = RunState()
        self._thread: threading.Thread | None = None
        #: Set under ``_lock`` from the moment a worker is committed to until it is
        #: actually running, closing the window where ``Thread.is_alive()`` is still False
        #: for an assigned-but-unstarted thread. See :meth:`_in_flight`.
        self._reserved = False
        self._driver: Any = None
        self._stop_requested = False

    # ── progress sink (called from the worker thread) ─────────────────────────

    def _on_progress(self, event: dict[str, Any]) -> None:
        """Absorb one driver progress event into :class:`RunState`.

        Runs on the WORKER thread, so every field it touches is written under the lock.
        Total-ordering matters for the counters: ``cycle`` may arrive without a stage and
        vice versa, so each key is applied independently rather than replacing the state.
        """
        with self._lock:
            st = self._state
            if "cycle" in event:
                try:
                    st.cycle = int(event["cycle"])
                except (TypeError, ValueError):
                    pass
            if "stage" in event:
                st.stage = str(event.get("stage") or "")
            if isinstance(event.get("preflight"), dict):
                st.preflight = dict(event["preflight"])
            if isinstance(event.get("budget"), dict):
                st.budget = dict(event["budget"])
            if isinstance(event.get("quiescence"), dict):
                st.quiescence = dict(event["quiescence"])
            if isinstance(event.get("cr_filed"), dict):
                st.drafted += 1
            st.activity.append({"t": time.time(), **_redact_activity(event)})

    def _on_agent_activity(self, event: dict[str, Any]) -> None:
        """Agent-turn feed (tool uses, assistant text) — the same ring buffer.

        Without this the UI shows "proposing fixes…" for twenty minutes with no sign
        the agent is alive. Tagged so the frontend can render it differently.
        """
        with self._lock:
            self._state.activity.append({"t": time.time(), "agent": _redact_activity(event)})

    # ── construction (blocking; called from the route's worker thread) ────────

    def _build_runner(self, *, stop_check) -> Any:
        """Pick the agent runner: the in-process provider when one is configured,
        else the ``claude -p`` subprocess, else None (offline spine — no fabricated fixes).

        ``import kiro_crew.acp`` FIRST: there is a known circular import that only
        resolves when the acp package is imported before ``create_provider_factory()``
        is reached. Importing it here, ahead of the availability probe, is what keeps
        this from failing on a cold gateway.
        """
        import kiro_crew.acp  # noqa: F401  # circular import — must precede the factory

        from ..spine.agent_runner import SessionAgentRunner

        if SessionAgentRunner.available():
            # CREDENTIAL CONFINEMENT PRECONDITION. The subprocess path spawns through
            # `sandboxed_spawn_argv(mode="strict")` + `strip_credential_env`, which hides
            # `~/.aws`, `~/.gnupg`, `gh`/`gcloud`/`kube` config and scrubs the token env. The
            # PROVIDER path does not: it drives a Kiro Crew session, so isolation is whatever
            # `cfg.sandbox` says — and that field DEFAULTS TO "off" ("defers isolation to
            # kiro-cli's internal agent sandbox"). On a gateway where kiro-cli provides no
            # sandbox, an injected repository instruction reaching the agent's auto-approved
            # Bash (`python helper.py`) could read those credential stores and exfiltrate over
            # an unrestricted network. Refuse rather than run unconfined: `None` means OFFLINE
            # (no fabricated fixes), which is the same fail-closed answer this method already
            # gives when the tool-restricted agent cannot be registered. Raised by the GPT
            # review. The watcher path is gated separately and explicitly
            # (`pr_watchers._watcher_egress_accepted`, D-118) because it genuinely needs `gh`
            # network access; the loop's authoring agent does not.
            unconfined = _credentials_are_unconfined()
            if unconfined:
                logger.warning(
                    "%s: refusing the provider-backed agent runner — %s, so an agent-run "
                    "command could read credential stores and exfiltrate. Running OFFLINE. "
                    "Set the gateway's `sandbox` to 'auto' to re-enable the OS-level sandbox, "
                    "or set `acceptUnsandboxedAgentRisk` to acknowledge the residual risk.",
                    store.APP_NAME,
                    unconfined,
                )
                return None
            runner = SessionAgentRunner(stop_check=stop_check, on_activity=self._on_agent_activity)
            # Register the tool-restricted discovery agent so kiro-cli resolves it by name.
            # FAIL CLOSED on the returned bool: an unknown agent name does not error, it
            # silently activates the DEFAULT agent — which carries the full kirocrew-core
            # toolset including `spawn_sub_agents`. Ignoring this result meant an
            # unwritable agent dir quietly widened an unattended agent's tool scope to
            # everything, which is the opposite of what registering it is for. Raised by
            # review of this branch.
            if not runner.ensure_agent_registered():
                # OFFLINE, not the subprocess fallback. This used to fall through, which
                # meant a configured provider whose agent registration failed silently
                # downgraded to `claude -p` — bypassing the provider's own permission
                # gate even though a provider EXISTED. Measured: with `available()` True
                # and `ensure_agent_registered()` False, `_build_runner` returned
                # `AgentRunner`. That is the substance of the review's long-standing
                # "fallback bypasses the ACP gate" objection, and it is a real hole
                # rather than the impossible case it looked like: the fallback is only
                # defensible when there is NO provider to route through. Raised by the
                # GPT review of this branch.
                logger.warning(
                    "%s: could not register the tool-restricted agent — running OFFLINE "
                    "rather than falling back to the subprocess agent, because a provider "
                    "is configured and its permission gate must not be bypassed",
                    store.APP_NAME,
                )
                return None
            return runner
        # NO subprocess fallback. Review asked for this removal on every head, and after the
        # two fall-through holes were closed the remaining objection turned out to be right on
        # the facts: the fallback's stated purpose — "the only path that authors fixes when no
        # in-process provider is configured" — describes a state that cannot occur.
        # `SessionAgentRunner.available()` is `cfg.create_provider_factory() is not None`, and
        # `create_provider_factory` has exactly two returns (`AcpProvider(...)` and `_acp`) and
        # NEVER returns None — verified by inspecting its source. So `available()` is False only
        # when the config load or the factory RAISES, i.e. a broken install rather than an
        # unconfigured one, and in that state shelling out to `claude -p` with
        # `--dangerously-skip-permissions` is the wrong answer anyway: it runs an unattended
        # agent outside the provider's permission gate precisely when the platform is unhealthy.
        # Running OFFLINE (no fabricated fixes) is the honest outcome. Removing the selection
        # rather than the class keeps `AgentRunner` available for a future caller that can route
        # it properly. Raised by the GPT review of this branch.
        logger.warning(
            "%s: no provider-backed agent runner available — running offline (the subprocess "
            "fallback is deliberately not used: it would bypass the provider permission gate)",
            store.APP_NAME,
        )
        return None

    def _build_driver(self, config: dict[str, Any]) -> Any:
        """Build the profile + driver for ``config``. Raises on a refusal condition.

        Every path in here is blocking (git, config load, provider probe), which is why
        the routes call ``start`` through :func:`asyncio.to_thread`.

        Holds the CLONE LOCK for the duration. This function runs ``git checkout -B`` on
        the shared clone — the exact operation ``commit.clone_lock`` exists to serialize —
        and the run-status gate is not a substitute: a run is not yet "running" while this
        is still doing git work, so a Start click could land inside the draft route's
        materialize → commit → draft window and move HEAD under it. The lock is an
        ``RLock`` precisely so a caller that already holds it does not self-deadlock here —
        which is what lets :meth:`_calibrate_loop` hold the lock across its whole body and
        still reach the profile build. Note this method's lock does NOT reach that path on
        its own: calibration has its own checkout and does not call through here.
        Raised by the GPT review.
        """
        from ..profiles import build_profile
        from ..spine.driver import BudgetCaps, Driver
        from .commit import clone_lock

        with clone_lock():
            return self._build_driver_locked(config, build_profile, BudgetCaps, Driver)

    def _build_driver_locked(
        self, config: dict[str, Any], build_profile: Any, BudgetCaps: Any, Driver: Any
    ) -> Any:
        """The body of :meth:`_build_driver`, which owns the clone lock. Split out so the
        lock acquisition is a single visible statement rather than an indent over 100
        lines — and so a future edit cannot add an early ``return`` that skips it."""

        # Put the clone on the CONFIGURED branch BEFORE building the profile. A fresh
        # clone sits on the repo default (main); a run targeting another branch would
        # otherwise measure and try to fix the WRONG tree — dogfooding on
        # feat/auto-improvement-app against a main clone matched zero allowlisted files
        # because main has no app subtree.
        #
        # Ordering is load-bearing, not cosmetic: the profile resolves `scopeDiffBase`
        # in its CONSTRUCTOR via `scoped_relpaths(clone, base)`, which diffs
        # `base...HEAD`. Built before the checkout, HEAD is still the default branch, so
        # that diff comes back empty, `scoped_relpaths` returns None ("no scope"), and
        # the edit fence silently widens from "what this branch changed" to the whole
        # repo. Checking out first makes the scope reflect the branch actually under
        # test. Raised by review of this branch.
        clone_dir = str(config.get("clone") or "").strip()
        branch = str(config.get("branch") or "").strip() or "main"
        if clone_dir:
            # Best-effort: log a failure but still start, EXCEPT when a diff scope was
            # requested — there, a failed checkout would compute the scope against the
            # wrong HEAD, and silently running unscoped is the outcome the operator was
            # trying to avoid by setting it.
            ok, note = clone_setup.checkout_branch(Path(clone_dir), branch)
            if not ok:
                # RAISE on any failed checkout, not only when scopeDiffBase is set.
                # `checkout_branch` already tries the remote-tracking ref AND a local ref
                # (see its own fallbacks), so a False here means the configured branch is
                # reachable NOWHERE — and starting an edit-and-push loop against whatever
                # HEAD the clone happens to hold would discover, edit, and push the WRONG
                # branch's code, the exact silent-wrong-branch harm the checkout exists to
                # prevent. A scopeDiffBase makes it worse (a mis-scoped edit fence), but the
                # base case is already unsafe. Raised by the GPT review of this branch.
                scoped = str(config.get("scopeDiffBase") or "").strip()
                extra = (
                    " and the configured scopeDiffBase would resolve against the wrong HEAD"
                    if scoped
                    else ""
                )
                raise RuntimeError(
                    f"refusing to start: could not check out {branch} ({note}) — the run "
                    f"would operate on the wrong revision{extra}"
                )
            logger.info("%s: %s", store.APP_NAME, note)

        profile = build_profile(config)  # raises ValueError when no clone is configured

        # The #1 safety control, re-asserted before anything is spawned. Same predicate
        # the driver uses, so the two checks cannot drift apart.
        if not profile.isolation.push_disabled():
            raise PermissionError(
                "refusing to start: the clone's push is not disabled — re-run repository setup"
            )

        caps = BudgetCaps(
            max_cycles=_pos_int(config.get("maxCycles"), DEFAULT_MAX_CYCLES),
            max_hours=_pos_float(config.get("maxHours"), DEFAULT_MAX_HOURS),
            max_cost_usd=_pos_float(config.get("maxCostUsd"), DEFAULT_MAX_COST_USD),
            # Consecutive no-keep cycles before the run calls the region mined out.
            # Exposed because it, not maxCycles, is the RIGHT way to end a run early:
            # it stops when there is nothing left to find instead of at an arbitrary
            # count. Left at the spine's default (3) unless configured.
            quiesce_after=_pos_int(config.get("quiesceAfter"), BudgetCaps.quiesce_after),
            # One wide + one deep proposal per cycle by default: each deep proposal is a
            # real agent call, and a 6-wide fan-out on a first run spends six times the
            # money to learn the same thing about whether the loop works at all.
            proposer_wide=_opt_int(config.get("proposerWide"), 1),
            proposer_deep=_opt_int(config.get("proposerDeep"), 1),
            measure_reps=_opt_int(config.get("measureReps"), None),
            reproduce_reps=_opt_int(config.get("reproduceReps"), None),
            band_cap_ms=_opt_float(config.get("bandCapMs"), None),
        )

        stop_check = self._stop_check
        agent_runner = self._build_runner(stop_check=stop_check)

        scratch = store.scratch_dir()
        return Driver(
            profile=profile,
            clone=Path(profile.clone_path),
            branch=branch,
            archive_root=store.results_dir(),
            ledger_path=store.ledger_path(),
            pr_queue_dir=store.pr_queue_dir(),
            # Worktrees are disposable and can be large — scratch, never the data dir
            # (which the do-not-pollute snapshot watches).
            worktree_root=scratch / "worktrees",
            caps=caps,
            on_progress=self._on_progress,
            agent_runner=agent_runner,
            logger=logging.getLogger("auto_improvement.driver"),
            # STRICT by default (03_metric §7.1): a canary that does not clear the band
            # means the ruler was never proven on this target, and a perf "win" measured
            # by an unproven ruler is exactly the unmeasured change this app exists to
            # refuse. It defaulted to advisory on the argument that strictness would also
            # halt bug-track runs — but `Driver.run` skips Phase-1 preflight entirely for
            # the bug track, so a strict canary never reaches one. With no cost to pay,
            # the contract wins; the flag stays as an explicit operator opt-out for a
            # target whose suite genuinely cannot force a measurable win.
            canary_advisory=_as_bool(config.get("canaryAdvisory"), False),
            direct_commit=_as_bool(config.get("directCommit"), False),
        )

    def _in_flight(self) -> bool:
        """Whether a run or calibration owns the supervisor. Caller must hold ``_lock``.

        Checks an explicit RESERVATION as well as thread liveness, because
        ``Thread.is_alive()`` is False for a thread that has been created and assigned but not
        yet ``start()``ed — and both entry points assigned under the lock and started after
        releasing it. In that window every liveness guard read "inactive", so two concurrent
        requests could both pass and two workers would mutate the same clone and overwrite
        each other's ``RunState``. Verified directly: an unstarted thread reports not-alive.
        Raised by the GPT review.
        """
        if self._reserved:
            return True
        return self._thread is not None and self._thread.is_alive()

    def _stop_check(self) -> bool:
        with self._lock:
            return self._stop_requested

    # ── the public API the routes call ────────────────────────────────────────

    def start(self, config: dict[str, Any]) -> dict[str, Any]:
        """Build the driver and launch the loop on a worker thread.

        Returns ``{"run_id", "status"}`` on success. Raises :class:`RuntimeError` when a
        run is already active, :class:`ValueError` when no repository is configured, and
        :class:`PermissionError` when the clone's push is not disabled — the routes map
        each to a 409 with the message intact.

        Order matters: the driver is built (and every refusal raised) BEFORE the thread
        is created, so a refusal leaves the supervisor exactly as it was.
        """
        with self._lock:
            if self._in_flight():
                raise RuntimeError(f"a run is already active (run_id={self._state.run_id})")

        driver = self._build_driver(config)
        run_id = f"run-{int(time.time())}"

        with self._lock:
            # Re-check under the lock: two concurrent POSTs could both pass the probe
            # above while the slow _build_driver ran outside it.
            if self._in_flight():
                raise RuntimeError(f"a run is already active (run_id={self._state.run_id})")
            self._driver = driver
            self._stop_requested = False
            self._state = RunState(
                status=STATUS_RUNNING,
                run_id=run_id,
                started_at=time.time(),
            )
            self._state.activity.append({"t": time.time(), "note": f"run {run_id} starting"})
            thread = threading.Thread(
                target=self._run_loop,
                args=(driver,),
                name=f"auto-improvement-{run_id}",
                daemon=True,  # never block gateway shutdown on a long measurement
            )
            self._thread = thread
            self._reserved = True
        try:
            thread.start()
        except BaseException:
            # A spawn failure must not wedge the supervisor as permanently busy.
            with self._lock:
                self._reserved = False
            raise
        return {"run_id": run_id, "status": STATUS_RUNNING}

    def calibrate(self, config: dict[str, Any]) -> dict[str, Any]:
        """Run Phase 1 — prove the ruler — on a worker thread.

        Collects untouched-baseline samples through the profile's ruler, computes
        ``noise_band = max(2sigma, floor)``, then forces the canary (a known win)
        and requires it to clear that band. Writes ``ruler/ruler.json`` either
        way: ``calibrated`` on success, ``canary_failed`` on an untrustworthy
        harness. Reporting a failed canary honestly is the point — a run that
        optimizes against a metric it cannot trust produces confident nonsense.

        Refuses when anything is already in flight, mirroring :meth:`start`, so
        two calibrations cannot interleave writes to the same ruler file.
        """
        with self._lock:
            if self._in_flight():
                raise RuntimeError(f"a run is already active (run_id={self._state.run_id})")

        run_id = f"cal-{int(time.time())}"
        with self._lock:
            if self._in_flight():
                raise RuntimeError(f"a run is already active (run_id={self._state.run_id})")
            self._stop_requested = False
            self._state = RunState(
                status=STATUS_CALIBRATING, run_id=run_id, started_at=time.time(), stage="calibrate"
            )
            self._state.activity.append(
                {"t": time.time(), "note": f"calibration {run_id} starting"}
            )
            thread = threading.Thread(
                target=self._calibrate_loop,
                args=(dict(config or {}),),
                name=f"auto-improvement-{run_id}",
                daemon=True,
            )
            self._thread = thread
            self._reserved = True
        try:
            thread.start()
        except BaseException:
            # A spawn failure must not wedge the supervisor as permanently busy.
            with self._lock:
                self._reserved = False
            raise
        return {"run_id": run_id, "status": STATUS_CALIBRATING}

    def _calibrate_loop(self, config: dict[str, Any]) -> None:
        """Worker body. Catches everything: an escaping exception would leave the
        UI reporting ``calibrating`` forever."""
        with self._lock:
            # The thread is RUNNING now, so `is_alive()` can carry the answer from here and the
            # reservation is no longer needed. Released first so an early return or raise below
            # cannot leave the supervisor permanently busy.
            self._reserved = False
        try:
            from .commit import clone_lock

            # The WHOLE body under the clone lock, not just the checkout. Calibration is the
            # longest clone-holding operation in the app: a checkout followed by
            # `baseline_samples` running the target's entire suite N times. Locking only the
            # checkout would look correct while leaving the part that actually needs a stable
            # tree exposed — a manual draft mutating the clone mid-baseline yields a ruler
            # calibrated against two different revisions. `_build_driver`'s lock does NOT
            # cover this path: calibration has its own checkout and never goes through it.
            # Raised by the GPT review.
            with clone_lock():

                from ..profiles import build_profile
                from ..spine.preflight import compute_noise_band
                from . import progress as progress_mod
                from . import store as store_mod

                # Same ordering requirement as `_build_driver`: calibration MEASURES the
                # suite, so it has to measure the branch the run will actually work on. A
                # baseline and noise band collected on the default branch would then be used
                # to judge candidates on a feature branch — comparing against a ruler built
                # from different code. Best-effort here (a failed checkout still calibrates
                # against current HEAD, as before) because calibration writes a ruler the
                # operator can inspect and re-run, rather than starting an edit loop.
                clone_dir = str(config.get("clone") or "").strip()
                branch = str(config.get("branch") or "").strip() or "main"
                if clone_dir:
                    ok, note = clone_setup.checkout_branch(Path(clone_dir), branch)
                    if not ok:
                        # RAISE rather than calibrate against an arbitrary HEAD: a ruler proven
                        # on the wrong branch's code is misleading, and `checkout_branch` only
                        # returns False when the branch exists nowhere. The operator fixes the
                        # branch and re-runs. Raised by the GPT review of this branch.
                        raise RuntimeError(
                            f"refusing to calibrate: could not check out {branch} ({note}) — "
                            "the ruler would be proven against the wrong revision"
                        )

                profile = build_profile(config)
                clone = Path(str(config.get("clone") or ""))
                reps = _pos_int(config.get("calibrationReps"), 5)

                self._note(f"collecting {reps} baseline sample(s)")
                samples = profile.ruler.baseline_samples(base_src=clone, reps=reps)
                if not samples:
                    raise RuntimeError("the ruler returned no baseline samples")

                raw_cap = config.get("bandCapMs")
                cap: float | None = None
                if raw_cap not in (None, ""):
                    try:
                        cap = float(str(raw_cap))
                    except (TypeError, ValueError):
                        # A garbled cap must not abort calibration; an absent cap is the
                        # documented default (no ceiling on the measured band).
                        cap = None
                band = compute_noise_band(
                    samples,
                    floor=float(getattr(profile.calibration, "noise_floor", 0.0) or 0.0),
                    cap=cap,
                )
                self._note(f"noise band = {band}")

                self._note("running the canary (a known win must clear the band)")
                canary = profile.ruler.measure_canary(base_src=clone)
                observed = float(getattr(canary, "primary_delta", 0.0) or 0.0)
                # Reuse the SPINE's predicate rather than re-deriving it. This used to be
                # `abs(observed) > band`, which ignored both `canary.ok` and the ruler's improving
                # DIRECTION — so `POST /calibrate` wrote `status="calibrated"` for two cases that
                # prove the opposite. Measured against the spine's rule at band=10: a REGRESSION of
                # +25 (minimize) passed, and a measurement with `ok=False` passed. A canary is the
                # one measurement whose sign we know a priori, so direction-blindness here defeats
                # the whole point of proving the ruler. `_canary_clears_band` already handles ok /
                # None / direction and is what Phase-1 preflight uses; a second copy is exactly how
                # these two drifted apart. Raised by the GPT review of this branch.
                from ..spine.preflight import _canary_clears_band

                cleared = _canary_clears_band(
                    canary,
                    band=band,
                    direction=str(getattr(profile.ruler, "direction", "minimize") or "minimize"),
                )
                status = "calibrated" if cleared else "canary_failed"

                primary = {
                    "name": getattr(profile.ruler, "primary_name", ""),
                    "unit": getattr(profile.ruler, "unit", ""),
                    "direction": getattr(profile.ruler, "direction", "minimize"),
                    "label": getattr(profile.ruler, "primary_name", ""),
                }
                median = sorted(samples)[len(samples) // 2]
                ruler_doc = {
                    "status": status,
                    "primary": primary,
                    "noiseBand": {
                        "value": band,
                        "unit": primary["unit"],
                        "method": "max(2sigma, floor)",
                    },
                    "anchors": [{"name": "baseline", "value": median}],
                    "canary": {
                        "result": status,
                        "observedDelta": observed,
                        "clearedBand": cleared,
                    },
                    "battery": samples,
                    "measured": {"reps": len(samples), "median": median},
                }
                # Write to the workspace the worker was LAUNCHED for, not the one live config
                # names right now. `store_mod.ruler_dir()` re-reads config.json, and this runs
                # on a background thread: if the operator retargets (or starts another repo's
                # calibration) while this one measures, the bare call would drop this ruler into
                # a DIFFERENT workspace, overwriting its ruler with one calibrated on unrelated
                # code. Deriving the path from the captured `config` pins it. Raised by the GPT
                # review of this branch.
                ruler_path = (
                    store_mod.data_dir()
                    / "repos"
                    / store_mod.workspace_key(config)
                    / "ruler"
                    / "ruler.json"
                )
                ruler_path.parent.mkdir(parents=True, exist_ok=True)
                store_mod.write_json_atomic(ruler_path, ruler_doc)

                with self._lock:
                    self._state.preflight = {
                        "noiseBand": band,
                        "baselineReps": len(samples),
                        "canaryDelta": observed,
                        "canaryCleared": cleared,
                    }
                    self._state.status = STATUS_DONE if cleared else STATUS_ERROR
                    self._state.stage = ""
                    self._state.finished_at = time.time()
                    if not cleared:
                        # An untrustworthy ruler is a REPORTED outcome, not a crash: the
                        # operator needs to see that the harness cannot measure.
                        #
                        # NOT routed through `_fail`/`_redact_activity`, and safe not to be:
                        # every interpolated value is numeric by construction (`observed` is
                        # a `float(...)`, `band` comes from `compute_noise_band`), so this
                        # string cannot carry agent-influenced text. It is also not an
                        # exception. `_fail` is for messages built from `str(exc)`, which is
                        # where untrusted content actually enters. Marked so the structural
                        # guard can tell "reviewed and safe" from "missed".
                        self._state.error = (  # redaction-exempt: numeric-only message
                            f"canary did not clear the noise band (|{observed}| <= {band}) — "
                            "the harness cannot detect a known win, so no run is trustworthy"
                        )
                    self._state.activity.append(
                        {"t": time.time(), "note": f"calibration finished: {status}"}
                    )
                # Confirm the write landed the way a reader will see it — the ruler file
                # is the contract every other component gates on, so a silent
                # disagreement here would let a run start on an uncalibrated ruler.
                if progress_mod.ruler_calibrated() != cleared:
                    logger.error(
                        "%s: ruler.json disagrees with the calibration result (expected %s)",
                        store.APP_NAME,
                        cleared,
                    )
        except BaseException as exc:  # noqa: BLE001 - a failure is state, not a crash
            logger.exception("%s: calibration failed", store.APP_NAME)
            self._fail(exc)

    def _fail(self, exc: BaseException) -> None:
        """Record a TERMINAL failure: status, redacted message, finish time, feed entry.

        One helper for all three sites because the message crosses an egress boundary.
        ``status()`` puts ``error`` straight into the ``GET /run`` JSON and ``SetupPanel``
        renders it verbatim, while an exception message routinely quotes what failed — a git
        url, a subprocess argv, a path. When a run dies on an agent-influenced value holding
        a credential, that credential reached the browser: ``activity`` beside it was scanned,
        this field was not. ``_redact_activity`` is FAIL-CLOSED, so an unscannable message
        becomes a placeholder rather than being served raw; the exception TYPE is composed in
        afterwards so the operator can still tell what kind of failure it was. Raised by the
        GPT review.
        """
        detail = _redact_activity(str(exc))
        message = f"{type(exc).__name__}: {detail}"
        with self._lock:
            self._state.status = STATUS_ERROR
            self._state.error = message
            self._state.finished_at = time.time()
            self._state.activity.append({"t": time.time(), "error": message})

    def _note(self, text: str) -> None:
        """Append a progress note the UI's activity feed shows."""
        with self._lock:
            self._state.activity.append({"t": time.time(), "note": text})

    def _run_loop(self, driver: Any) -> None:
        """The worker thread body. Catches EVERYTHING: an escaping exception here would
        kill the thread silently and leave the UI reporting ``running`` forever."""
        with self._lock:
            # The thread is RUNNING now, so `is_alive()` can carry the answer from here and the
            # reservation is no longer needed. Released first so an early return or raise below
            # cannot leave the supervisor permanently busy.
            self._reserved = False
        try:
            stats = driver.run()
            with self._lock:
                self._state.stats = _stats_dict(stats)
                self._state.kept = int(getattr(stats, "kept", 0) or 0)
                self._state.status = STATUS_DONE
                self._state.stage = ""
                self._state.finished_at = time.time()
                self._state.activity.append({"t": time.time(), "note": "run finished"})
        except BaseException as exc:  # noqa: BLE001 — a run failure is state, not a crash
            logger.exception("%s: run failed", store.APP_NAME)
            self._fail(exc)

    def status(self) -> dict[str, Any]:
        """A JSON-ready snapshot. Cheap, lock-guarded, safe to poll on the event loop.

        ``activity`` is copied out as a list so the caller can serialize it without
        racing the worker thread's next append.
        """
        with self._lock:
            st = self._state
            alive = self._thread is not None and self._thread.is_alive()
            # A thread that died without setting a terminal status (only possible if the
            # interpreter tore it down) must not be reported as still running.
            status = st.status
            if status in (STATUS_RUNNING, STATUS_STOPPING) and not alive:
                status = STATUS_DONE if not st.error else STATUS_ERROR
            return {
                "status": status,
                "run_id": st.run_id,
                "cycle": st.cycle,
                "stage": st.stage,
                "kept": st.kept,
                "drafted": st.drafted,
                "error": st.error,
                "started_at": st.started_at,
                "finished_at": st.finished_at,
                "preflight": dict(st.preflight),
                "budget": dict(st.budget),
                "quiescence": dict(st.quiescence),
                "stats": dict(st.stats),
                "activity": list(st.activity),
            }

    def stop(self) -> dict[str, Any]:
        """Request a clean stop and wait, bounded, for the thread to finish.

        The spine stops BETWEEN candidates — it will not abandon a measurement midway —
        so this can return with the thread still winding down. That is reported honestly
        as ``stopping`` rather than waited out: holding an HTTP request open for a
        fifteen-minute suite would just time out at the proxy.
        """
        with self._lock:
            thread = self._thread
            driver = self._driver
            if thread is None or not thread.is_alive():
                return {
                    "status": self._state.status,
                    "run_id": self._state.run_id,
                    "stopped": False,
                    "note": "no active run",
                }
            self._stop_requested = True
            self._state.status = STATUS_STOPPING
            self._state.activity.append({"t": time.time(), "note": "stop requested"})
            run_id = self._state.run_id

        if driver is not None:
            try:
                driver.request_stop()
            except Exception:  # noqa: BLE001 — a stop must never raise at the caller
                logger.debug("request_stop failed", exc_info=True)

        thread.join(timeout=STOP_JOIN_TIMEOUT_S)
        stopped = not thread.is_alive()
        with self._lock:
            return {
                "status": self._state.status if stopped else STATUS_STOPPING,
                "run_id": run_id,
                "stopped": stopped,
            }


# ── config coercion ─────────────────────────────────────────────────────────
#
# Config arrives from JSON on disk, so a value can be a string, null, or nonsense.
# Each helper falls back to the default rather than raising: a bad config value should
# start a run with sane budgets, not 500 the Start button.


def _pos_int(value: Any, default: int) -> int:
    try:
        out = int(value)
    except (TypeError, ValueError):
        return default
    return out if out > 0 else default


def _pos_float(value: Any, default: float) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if out > 0 else default


def _opt_int(value: Any, default: int | None) -> int | None:
    try:
        out = int(value)
    except (TypeError, ValueError):
        return default
    return out if out > 0 else default


def _opt_float(value: Any, default: float | None) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if out > 0 else default


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _stats_dict(stats: Any) -> dict[str, Any]:
    """Copy the spine's ``Stats`` into a plain dict (it is a dataclass, but this keeps
    the route's JSON independent of the spine's field set)."""
    keys = (
        "cycles",
        "discovered",
        "deduped",
        "gated_out",
        "not_kept",
        "kept",
        "filed",
        "errors",
        "cost_usd",
    )
    return {k: getattr(stats, k, 0) for k in keys}


# ── module singleton ────────────────────────────────────────────────────────
#
# One supervisor per gateway process: "is a run active?" must be a single answer, and
# routes are registered on the shared aiohttp app with no per-request state to hang it
# off. Created lazily under a lock so two concurrent first requests cannot each make one.

_SUPERVISOR: RunSupervisor | None = None
_SUPERVISOR_LOCK = threading.Lock()


def get_supervisor() -> RunSupervisor:
    """The process-wide :class:`RunSupervisor`."""
    global _SUPERVISOR
    with _SUPERVISOR_LOCK:
        if _SUPERVISOR is None:
            _SUPERVISOR = RunSupervisor()
        return _SUPERVISOR
