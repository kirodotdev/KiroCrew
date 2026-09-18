"""The recovery ladder: which layer retries, how often, and when it hands up.

Five rungs, tried bottom-up, each bounded (RFC §7):

======  ==============  ======================================  =================  =========  ===============
layer   unit            trigger                                 cleanup deadline   attempts   escalates to
======  ==============  ======================================  =================  =========  ===============
L1      tool call       JSON-RPC error classed                  none               3          L2
                        ``recoverable_infra`` (``-32001``
                        capacity, backend gone, queued timeout)
L2      backend         ``BackendGone``, initialize timeout,    per-backend        2          L3
                        breaker OPEN                            shutdown budget
L3      ACP runtime     ``AcpRuntimeDead``, stall past the      shutdown budget +  2          L4
                        idle window, ``session/new`` abandoned  process-tree kill
L4      gatewayd        liveness ping failed 3x (fast and       daemon drain       1 / 10min  L5
                        escalated probe both missed) AND no                        (2nd in
                        backend progress                                           window)
L5      gateway         none automatic                          --                 0          --
======  ==============  ======================================  =================  =========  ===============

Overload never enters the ladder: pressure lowers caps and pauses admission
(the adaptive controller); only a unit that stopped making progress AND failed
an independent probe is torn down. L5 is deliberately not automatic -- a
gateway restart is the user's decision -- so the ladder's top rung is a
notification, emitted once per escalation, never a ``kirocrew restart``.

Every layer reads its schedule from one :class:`RecoveryPolicy`, so a change to
the base or the cap moves every layer together and the layers stay
de-correlated by jitter rather than by luck. :data:`LADDER` is that schedule's
static form and the literals below are its defaults; the process ladder carries
the CONFIGURED policy from the moment :func:`configure_default_ladder` snapshots
``agent.recovery_backoff_*`` at boot, so a configured install does not run the
numbers in this table.

:func:`classify_infra_error` is the L1 detector: it recognises the MCP stub's
``-32001 capacity`` refusal (with its ``retry_after_secs``) and the small closed
set of gateway ``recoverable_infra`` markers, and nothing else -- an ordinary
tool result that happens to mention a backend must not be retried as an
infrastructure failure.
"""

from __future__ import annotations

import json
import logging
import random
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from kiro_crew.mcp_gateway.shutdown_budget import (
    POOL_SHUTDOWN_SECS,
    TOTAL_SHUTDOWN_BUDGET_SECS,
)
from kiro_crew.metrics.events import (
    RECOVERY_ATTEMPTS,
    RECOVERY_DURATION_SECS,
    RECOVERY_ESCALATIONS,
    RESTARTS_TOTAL,
    emit_counter,
    emit_histogram,
)
from kiro_crew.recovery.policy import (
    DEFAULT_COOLDOWN_SECS,
    LayerPolicy,
    RecoveryPolicy,
    RecoveryTracker,
)

logger = logging.getLogger(__name__)

# ── layers ───────────────────────────────────────────────────────────────────

L1_TOOL_CALL = "L1_tool_call"
L2_BACKEND = "L2_backend"
L3_ACP_RUNTIME = "L3_acp_runtime"
L4_GATEWAYD = "L4_gatewayd"
L5_GATEWAY = "L5_gateway"

#: Bottom-up order. A metric attribute value set: closed, five members.
LAYERS: tuple[str, ...] = (L1_TOOL_CALL, L2_BACKEND, L3_ACP_RUNTIME, L4_GATEWAYD, L5_GATEWAY)

#: Decision actions -- another closed set (metric attribute values).
ACTION_RETRY = "retry"
ACTION_ESCALATE = "escalate"
ACTION_NOTIFY = "notify"
ACTION_GIVE_UP = "give_up"

#: Error classes :func:`classify_infra_error` can return.
CLASS_CAPACITY = "capacity"
CLASS_RECOVERABLE_INFRA = "recoverable_infra"

#: The JSON-RPC code the MCP stub serves for a capacity refusal
#: (``mcp_gateway.stub._serve_capacity_refusal``). Named here rather than
#: imported so the L1 detector never pays the stub's import.
JSONRPC_CAPACITY_CODE = -32001

#: A daemon respawn is one rung; a second within this window is the L4 -> L5
#: escalation (RFC: "1 per 10 min").
GATEWAYD_RESPAWN_COOLDOWN_SECS = 600.0

#: The gatewayd supervisor's backoff floor and cap. The floor is 1s because a
#: daemon respawn is a cold start whose first retry is cheap and whose stubs are
#: already waiting. The cap is BELOW the shared 120s default and pinned: the
#: stub's 600s reconnect budget is sized as three kill->respawn cycles that
#: each include this cap (``test_stub_reconnect_budget`` pins the mirror
#: through ``manager._RESPAWN_BACKOFF_MAX_SECS``), so raising it here would
#: silently shorten the outage the stub survives.
GATEWAYD_BACKOFF_BASE_SECS = 1.0
GATEWAYD_BACKOFF_MAX_SECS = 60.0

#: L3's IN-PLACE budget: how many times one ACP session is continued on the
#: same runtime before the session is declared wedged -- the main chat's
#: tool-stall / stale_recover continue-nudges and its pipe-death re-queues, and
#: the sub-agent run's stop-recovery nudges, all count against this one number
#: (``acp.types.STOP_RECOVERY_MAX_RETRIES`` re-exports it). It is distinct from
#: :data:`LADDER`'s L3 ``max_attempts``: that counts RUNTIME rebuilds before the
#: ladder escalates to L4, while this counts turns continued inside a runtime
#: that is still alive. Both live here so the two budgets are read side by side.
SESSION_RECOVERY_MAX_ATTEMPTS = 3

LADDER: RecoveryPolicy = RecoveryPolicy(
    layers={
        L1_TOOL_CALL: LayerPolicy(
            layer=L1_TOOL_CALL,
            trigger=(
                "JSON-RPC error classed recoverable_infra: -32001 capacity, "
                "backend gone, spawn-queue timeout"
            ),
            max_attempts=3,
            cleanup_deadline_secs=None,
            escalate_to=L2_BACKEND,
        ),
        L2_BACKEND: LayerPolicy(
            layer=L2_BACKEND,
            trigger="BackendGone, initialize timeout, breaker OPEN",
            max_attempts=2,
            cleanup_deadline_secs=POOL_SHUTDOWN_SECS,
            escalate_to=L3_ACP_RUNTIME,
        ),
        L3_ACP_RUNTIME: LayerPolicy(
            layer=L3_ACP_RUNTIME,
            trigger=(
                "AcpRuntimeDead, stall past subagent_stall_idle_secs, "
                "session/new collector abandoned"
            ),
            max_attempts=2,
            cleanup_deadline_secs=TOTAL_SHUTDOWN_BUDGET_SECS,
            escalate_to=L4_GATEWAYD,
        ),
        L4_GATEWAYD: LayerPolicy(
            layer=L4_GATEWAYD,
            trigger=(
                "liveness ping failed 3x (neither the fast nor the escalated "
                "probe answered) AND no backend progress for 60s"
            ),
            # "1 per 10 min": the first respawn inside the cooldown is the
            # allowed one; a second failure while it is still counted is what
            # escalates (attempts >= 2).
            max_attempts=2,
            base_secs=GATEWAYD_BACKOFF_BASE_SECS,
            max_secs=GATEWAYD_BACKOFF_MAX_SECS,
            cooldown_secs=GATEWAYD_RESPAWN_COOLDOWN_SECS,
            pinned=True,
            cleanup_deadline_secs=TOTAL_SHUTDOWN_BUDGET_SECS,
            escalate_to=L5_GATEWAY,
        ),
        L5_GATEWAY: LayerPolicy(
            layer=L5_GATEWAY,
            trigger="none automatic: escalation event + notification, the user restarts",
            max_attempts=0,
            cleanup_deadline_secs=None,
            escalate_to=None,
            automatic=False,
        ),
    }
)


# ── L1 detector ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class InfraError:
    """A tool failure the ladder may retry: its class and the server's retry hint."""

    error_class: str
    retry_after_secs: float | None = None
    code: int | None = None
    detail: str = ""


#: Above this length a tool result is a document, not an error envelope: the
#: capacity refusal is one JSON-RPC error object and every gateway marker is a
#: one-line message. A file dump that quotes one of the markers must not be
#: read as a failure of the call that read it.
_INFRA_TEXT_MAX_CHARS = 2000

_CAPACITY_CODE_RE = re.compile(r"(?<![\d.])-32001(?![\d.])")
_CAPACITY_CLASS_RE = re.compile(r"""["']?class["']?\s*[:=]\s*["']?capacity\b""", re.IGNORECASE)
_RETRY_AFTER_RE = re.compile(r"""retry_after_secs["']?\s*[:=]\s*["']?(\d+(?:\.\d+)?)""")

#: The gateway's own ``recoverable_infra`` markers. Closed set; each is the
#: exact phrase the emitting site logs or serves, not a paraphrase.
_RECOVERABLE_INFRA_MARKERS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bBackendGone\b"),
    re.compile(r"\bbackend (?:is )?gone\b", re.IGNORECASE),
    re.compile(r"\bSpawnGateTimeout\b"),
    re.compile(r"\bspawn[- ]queue (?:wait )?timed? ?out\b", re.IGNORECASE),
    re.compile(r"\bqueued timeout\b", re.IGNORECASE),
    re.compile(r"\bgateway daemon (?:is )?unavailable\b", re.IGNORECASE),
    re.compile(r"\breconnect budget exhausted\b", re.IGNORECASE),
)


def _classify_error_object(err: Mapping[str, Any]) -> InfraError | None:
    code = err.get("code")
    data = err.get("data")
    klass = data.get("class") if isinstance(data, Mapping) else None
    retry_after: float | None = None
    if isinstance(data, Mapping):
        raw = data.get("retry_after_secs")
        if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw >= 0:
            retry_after = float(raw)
    message = str(err.get("message") or "")
    if code == JSONRPC_CAPACITY_CODE or klass == CLASS_CAPACITY:
        return InfraError(
            CLASS_CAPACITY,
            retry_after_secs=retry_after,
            code=code if isinstance(code, int) else JSONRPC_CAPACITY_CODE,
            detail=message[:200],
        )
    if message and any(p.search(message) for p in _RECOVERABLE_INFRA_MARKERS):
        return InfraError(
            CLASS_RECOVERABLE_INFRA,
            retry_after_secs=retry_after,
            code=code if isinstance(code, int) else None,
            detail=message[:200],
        )
    return None


def classify_infra_error(payload: Any) -> InfraError | None:
    """Return the :class:`InfraError` a tool failure represents, or None.

    ``payload`` is whatever the tool-result path has: a JSON-RPC error object
    (``{"code", "message", "data"}``), an exception, or the result text kiro-cli
    put on the ``tool_call_update``. Only the closed set of infrastructure
    shapes is recognised; anything else -- including a permission denial, an
    invalid argument, a model refusal, or a long document that quotes a marker
    -- returns None, because retrying those repeats them.
    """
    if payload is None:
        return None
    if isinstance(payload, Mapping):
        inner = payload.get("error")
        if isinstance(inner, Mapping):
            return _classify_error_object(inner)
        return _classify_error_object(payload)
    if isinstance(payload, BaseException):
        text = str(payload)
    else:
        text = str(payload)
    text = text.strip()
    if not text or len(text) > _INFRA_TEXT_MAX_CHARS:
        return None
    # A serialised error object first: it is the shape the stub actually sends.
    if text.startswith("{"):
        try:
            obj = json.loads(text)
        except ValueError:
            obj = None
        if isinstance(obj, Mapping):
            found = classify_infra_error(obj)
            if found is not None:
                return found
    retry_after: float | None = None
    m = _RETRY_AFTER_RE.search(text)
    if m:
        try:
            retry_after = float(m.group(1))
        except ValueError:
            retry_after = None
    if _CAPACITY_CODE_RE.search(text) or _CAPACITY_CLASS_RE.search(text):
        return InfraError(
            CLASS_CAPACITY,
            retry_after_secs=retry_after,
            code=JSONRPC_CAPACITY_CODE,
            detail=text[:200],
        )
    if any(p.search(text) for p in _RECOVERABLE_INFRA_MARKERS):
        return InfraError(CLASS_RECOVERABLE_INFRA, retry_after_secs=retry_after, detail=text[:200])
    return None


# ── decisions ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RecoveryDecision:
    """What the ladder wants done about one failure at one layer."""

    layer: str
    unit: str
    action: str
    attempt: int
    delay_secs: float = 0.0
    next_layer: str | None = None
    reason: str = ""

    @property
    def retry(self) -> bool:
        return self.action == ACTION_RETRY


EventSink = Callable[[str, dict[str, Any]], None]
Notifier = Callable[[str, str], None]


def _log_notifier(layer: str, message: str) -> None:
    logger.error("recovery ladder escalation to %s: %s", layer, message)


class RecoveryLadder:
    """Stateful driver over :data:`LADDER`: counts, decides, escalates, measures.

    One instance per process is enough (:func:`default_ladder`); layers key
    their units so the counts never collide. ``event_sink`` receives one
    ``("recover", {...})`` per decision for the task store's ``task_events``
    when the caller can name a task; ``notifier`` receives the single L5
    message per escalation. Both default to logging. ``clock``/``rng`` are
    injectable for tests.
    """

    def __init__(
        self,
        policy: RecoveryPolicy = LADDER,
        *,
        event_sink: EventSink | None = None,
        notifier: Notifier | None = None,
        clock: Callable[[], float] = time.monotonic,
        rng: random.Random | None = None,
    ) -> None:
        self._policy = policy
        self._event_sink = event_sink
        self._notifier = notifier or _log_notifier
        self._clock = clock
        self._rng = rng
        self._lock = threading.Lock()
        self._trackers: dict[str, RecoveryTracker] = {
            name: RecoveryTracker(cooldown_secs=lp.cooldown_secs)
            for name, lp in policy.layers.items()
        }
        # (layer, unit) pairs whose escalation notice was already sent in the
        # current run of failures; cleared by success so the next incident
        # notifies again.
        self._notified: set[tuple[str, str]] = set()

    @property
    def policy(self) -> RecoveryPolicy:
        return self._policy

    def layer_policy(self, layer: str) -> LayerPolicy:
        return self._policy.layer(layer)

    def attempts(self, layer: str, unit: str, *, now: float | None = None) -> int:
        now = self._clock() if now is None else now
        with self._lock:
            return self._trackers[layer].attempts(unit, now)

    def backoff_secs(
        self, layer: str, attempt: int, *, retry_after_secs: float | None = None
    ) -> float:
        """The jittered delay for ``attempt`` at ``layer`` -- no state change."""
        return self._policy.layer(layer).backoff_secs(
            attempt, retry_after_secs=retry_after_secs, rng=self._rng
        )

    def observe_failure(
        self,
        layer: str,
        unit: str,
        *,
        now: float | None = None,
        retry_after_secs: float | None = None,
        reason: str = "",
        task_id: str | None = None,
    ) -> RecoveryDecision:
        """Record one failure of ``unit`` at ``layer`` and say what to do next.

        ``retry`` with a jittered ``delay_secs`` (floored by ``retry_after_secs``)
        while the layer has attempts left; ``escalate`` naming ``next_layer``
        once they are spent; ``notify`` when the next layer is L5 (the notifier
        fires once per run of failures); ``give_up`` when the layer itself is
        the non-automatic top and nothing is above it.
        """
        lp = self._policy.layer(layer)
        now = self._clock() if now is None else now
        with self._lock:
            attempt = self._trackers[layer].record_failure(unit, now)
        if not lp.automatic:
            decision = RecoveryDecision(
                layer, unit, ACTION_GIVE_UP, attempt, reason=reason or lp.trigger
            )
            self._notify_once(layer, unit, reason or lp.trigger)
        elif not lp.should_escalate(attempt):
            delay = lp.backoff_secs(attempt, retry_after_secs=retry_after_secs, rng=self._rng)
            decision = RecoveryDecision(
                layer, unit, ACTION_RETRY, attempt, delay_secs=delay, reason=reason
            )
        else:
            nxt = lp.escalate_to
            if nxt is None:
                decision = RecoveryDecision(layer, unit, ACTION_GIVE_UP, attempt, reason=reason)
            elif not self._policy.layer(nxt).automatic:
                decision = RecoveryDecision(
                    layer, unit, ACTION_NOTIFY, attempt, next_layer=nxt, reason=reason
                )
                self._notify_once(
                    nxt,
                    unit,
                    f"{layer} exhausted {attempt} attempt(s) on {unit}; "
                    f"automatic recovery stops here ({reason or lp.trigger})",
                )
            else:
                decision = RecoveryDecision(
                    layer, unit, ACTION_ESCALATE, attempt, next_layer=nxt, reason=reason
                )
            if nxt is not None:
                emit_counter(RECOVERY_ESCALATIONS, {"from_layer": layer, "to_layer": nxt})
        emit_counter(RECOVERY_ATTEMPTS, {"layer": layer, "action": decision.action})
        self._sink(
            task_id,
            {
                "layer": layer,
                "attempt": attempt,
                "action": decision.action,
                "delay_secs": round(decision.delay_secs, 3),
                "next_layer": decision.next_layer,
                "reason": (reason or "")[:200],
            },
        )
        return decision

    def observe_success(
        self, layer: str, unit: str, *, now: float | None = None, task_id: str | None = None
    ) -> float | None:
        """``unit`` recovered at ``layer``: reset its count, measure the outage.

        Returns the recovery duration in seconds (first failure -> now) when a
        run of failures was open, else None. Emits ``recovery_duration_secs``.
        """
        now = self._clock() if now is None else now
        with self._lock:
            tracker = self._trackers[layer]
            since = tracker.failing_since(unit, now)
            tracker.record_success(unit)
            self._notified.discard((layer, unit))
            lp = self._policy.layer(layer)
            if lp.escalate_to is not None:
                self._notified.discard((lp.escalate_to, unit))
        if since is None:
            return None
        duration = max(0.0, now - since)
        emit_histogram(RECOVERY_DURATION_SECS, duration, {"layer": layer}, unit="s")
        self._sink(task_id, {"layer": layer, "action": "recovered", "duration_secs": duration})
        return duration

    def record_restart(self, layer: str) -> None:
        """Count one rebuild at ``layer`` (a backend respawn, a daemon respawn)."""
        if layer not in self._policy.layers:
            raise KeyError(f"unknown recovery layer {layer!r}")
        emit_counter(RESTARTS_TOTAL, {"layer": layer})

    def forget(self, layer: str, unit: str) -> None:
        with self._lock:
            self._trackers[layer].forget(unit)
            self._notified.discard((layer, unit))

    def _adopt_schedule(self, policy: RecoveryPolicy) -> None:
        """Swap in ``policy``, keeping the failure counts already recorded.

        Only :func:`configure_default_ladder` calls this, and only to put the
        configured schedule on a ladder a consumer built first: a failure
        counted a moment earlier is still a failure of the same unit, so its
        count must survive the swap. A tracker whose layer's decay window moved
        is rebuilt instead -- ``cooldown_secs`` is baked into the tracker at
        construction, so keeping it would decay counts on the old window.
        """
        with self._lock:
            self._policy = policy
            for name, lp in policy.layers.items():
                tracker = self._trackers.get(name)
                if tracker is None or tracker.cooldown_secs != lp.cooldown_secs:
                    self._trackers[name] = RecoveryTracker(cooldown_secs=lp.cooldown_secs)

    def table(self) -> list[dict[str, Any]]:
        """The ladder as rows (for ``kirocrew doctor`` / the health payload)."""
        rows = []
        for name in LAYERS:
            lp = self._policy.layer(name)
            rows.append(
                {
                    "layer": name,
                    "trigger": lp.trigger,
                    "cleanup_deadline_secs": lp.cleanup_deadline_secs,
                    "backoff_base_secs": lp.base_secs,
                    "backoff_max_secs": lp.max_secs,
                    "jitter": lp.jitter,
                    "attempts_before_escalation": lp.max_attempts,
                    "cooldown_secs": lp.cooldown_secs,
                    "escalates_to": lp.escalate_to,
                    "automatic": lp.automatic,
                }
            )
        return rows

    # ── internals ──

    def _notify_once(self, layer: str, unit: str, message: str) -> None:
        with self._lock:
            key = (layer, unit)
            if key in self._notified:
                return
            self._notified.add(key)
        try:
            self._notifier(layer, message)
        except Exception:  # a notifier must never break recovery
            logger.debug("recovery notifier failed", exc_info=True)

    def _sink(self, task_id: str | None, data: dict[str, Any]) -> None:
        if self._event_sink is None or not task_id:
            return
        try:
            self._event_sink(task_id, data)
        except Exception:  # the store is best-effort here; recovery goes on
            logger.debug("recovery event sink failed for %s", task_id, exc_info=True)


_default: RecoveryLadder | None = None
_default_configured = False
_default_lock = threading.Lock()


def default_ladder() -> RecoveryLadder:
    """The process-wide ladder; built lazily so importing this module is free.

    Its schedule is :data:`LADDER`'s defaults until
    :func:`configure_default_ladder` installs the configured one.
    """
    global _default
    with _default_lock:
        if _default is None:
            _default = RecoveryLadder()
        return _default


def configure_default_ladder(cfg: Any) -> RecoveryLadder:
    """Snapshot ``agent.recovery_backoff_*`` onto the process ladder, at boot.

    ``cfg`` is a config the caller already holds and is read through
    :meth:`RecoveryPolicy.from_config`'s ``getattr``, so this module still
    imports nothing from :mod:`kiro_crew.config`. Loading one here instead would
    put disk I/O and the loader's publish side effects on the event loop, since
    the first consumer to want a delay is a failure branch inside a running turn.

    Both keys are ``restart=True``, so ONE snapshot per process is the whole
    contract: a later call keeps the first schedule and the counts accumulated
    under it, and a knob change takes effect on the next start. A ladder an
    earlier :func:`default_ladder` already built adopts the configured schedule,
    so the snapshot does not depend on boot order. L4 is ``pinned`` and keeps
    its own 1s/60s -- the stub's reconnect budget is sized from that cap.
    """
    global _default, _default_configured
    policy = RecoveryPolicy.from_config(cfg)
    with _default_lock:
        if _default is None:
            _default = RecoveryLadder(policy)
        elif not _default_configured:
            _default._adopt_schedule(policy)
        _default_configured = True
        return _default


def _reset_default_ladder_for_tests() -> None:
    global _default, _default_configured
    with _default_lock:
        _default = None
        _default_configured = False


def record_restart(layer: str) -> None:
    """Module-level shortcut for emit sites that hold no ladder reference."""
    default_ladder().record_restart(layer)


__all__ = [
    "ACTION_ESCALATE",
    "ACTION_GIVE_UP",
    "ACTION_NOTIFY",
    "ACTION_RETRY",
    "CLASS_CAPACITY",
    "CLASS_RECOVERABLE_INFRA",
    "DEFAULT_COOLDOWN_SECS",
    "GATEWAYD_BACKOFF_BASE_SECS",
    "GATEWAYD_BACKOFF_MAX_SECS",
    "SESSION_RECOVERY_MAX_ATTEMPTS",
    "GATEWAYD_RESPAWN_COOLDOWN_SECS",
    "JSONRPC_CAPACITY_CODE",
    "L1_TOOL_CALL",
    "L2_BACKEND",
    "L3_ACP_RUNTIME",
    "L4_GATEWAYD",
    "L5_GATEWAY",
    "LADDER",
    "LAYERS",
    "InfraError",
    "RecoveryDecision",
    "RecoveryLadder",
    "classify_infra_error",
    "configure_default_ladder",
    "default_ladder",
    "record_restart",
]
