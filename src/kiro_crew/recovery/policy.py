"""The one retry schedule every recovery layer reads.

Before this module each layer carried its own literals -- the gatewayd
supervisor doubled from 1s to 60s, the ACP client slept a flat 2s, the task
store computed ``2 * 2**attempts`` capped at 120s, the stub had a mirror of the
supervisor's cap -- and none of them jittered. Under one incident every layer
therefore retried in lock-step on its own clock, which is the retry storm the
RFC's §7 exists to remove. A :class:`RecoveryPolicy` is that schedule; a
:class:`LayerPolicy` is the schedule specialised to one rung of the ladder
(attempt cap, cleanup deadline, whether escalation is automatic at all).

Design rules, each of which a test pins:

* **Exponential with a cap.** ``base * 2**(attempt-1)``, never above ``max``.
* **Equal jitter, not full jitter.** The delay is drawn from
  ``[raw/2, raw]``. Full jitter (``[0, raw]``) can draw a near-zero delay and
  hot-loop a layer whose failure is not transient yet; equal jitter keeps the
  spread that de-correlates the layers while guaranteeing a floor.
* **A server-stated ``retry_after`` is a floor, never ignored.** The stub's
  ``-32001 capacity`` error names when the daemon expects capacity; retrying
  before it is a wasted queue wait. The delay is ``max(retry_after, backoff)``
  and it stays under the cap so a hostile value cannot park a task forever.
* **Attempts are per unit and decay.** A :class:`RecoveryTracker` counts
  consecutive failures per unit key (a slot, a backend key, a runtime, the
  daemon) and forgets them once ``cooldown_secs`` pass without a failure, so a
  unit that recovered and fails again a day later starts at attempt 1 instead
  of being escalated on its first hiccup.
* **Pure.** No clock read, no randomness, no config import at module level.
  Callers pass ``now`` and may pass an ``rng``; the defaults are the module
  constants the RFC names (``agent.recovery_backoff_base_secs=2``,
  ``agent.recovery_backoff_max_secs=120``) and :meth:`RecoveryPolicy.from_config`
  reads those keys, through ``getattr``, from a config object the CALLER holds.
  Which is why the configured schedule is installed from outside this package:
  ``ladder.configure_default_ladder(cfg)`` at gateway start for every
  ``default_ladder()`` consumer, ``taskq.dependency.coordinator_from_config``
  for a dependency wait. Both derive from the same two keys, so a doctor run
  and a live retry cannot print different numbers for one knob.
"""

from __future__ import annotations

import random
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from typing import Any

#: Backoff base and cap (seconds) when no config overrides them. These are the
#: RFC §12 defaults for ``agent.recovery_backoff_base_secs`` /
#: ``agent.recovery_backoff_max_secs`` and the values every layer holds
#: as its own literal.
DEFAULT_BACKOFF_BASE_SECS = 2.0
DEFAULT_BACKOFF_MAX_SECS = 120.0

#: Bounds the config clamp applies to the two keys above.
BACKOFF_BASE_MIN_SECS = 0.1
BACKOFF_BASE_MAX_SECS = 60.0
BACKOFF_MAX_MIN_SECS = 1.0
BACKOFF_MAX_MAX_SECS = 3600.0

#: How long without a failure before a unit's attempt count is forgotten.
#: Wider than the widest backoff so a unit that is still inside its own retry
#: window can never be reset by the passage of that window alone.
DEFAULT_COOLDOWN_SECS = 600.0

#: Exponent cap: ``2**16`` seconds is already far above any allowed ``max``,
#: so a larger exponent changes nothing except risk an overflow on a corrupted
#: attempt count.
_MAX_EXPONENT = 16

#: Units the tracker remembers at once. A unit key is a slot key, a backend
#: key or a runtime id -- bounded populations -- but a runaway caller must not
#: be able to grow this without limit, so the oldest entry is evicted past this.
TRACKER_MAX_UNITS = 1024


@dataclass(frozen=True)
class LayerPolicy:
    """The schedule for one rung of the ladder.

    ``max_attempts`` is how many failures a unit may accumulate at this layer
    before the ladder escalates to ``escalate_to``; ``0`` means the layer is
    never retried automatically (L5). ``cleanup_deadline_secs`` is the budget
    the layer's cleanup step (drain, shutdown, process-tree kill) is given
    before the rebuild proceeds anyway; ``None`` means the layer has no
    cleanup step of its own (L1 re-issues a call, nothing to tear down).
    """

    layer: str
    trigger: str
    max_attempts: int
    base_secs: float = DEFAULT_BACKOFF_BASE_SECS
    max_secs: float = DEFAULT_BACKOFF_MAX_SECS
    jitter: bool = True
    cooldown_secs: float = DEFAULT_COOLDOWN_SECS
    cleanup_deadline_secs: float | None = None
    escalate_to: str | None = None
    #: False for the one layer whose action is only to tell a human.
    automatic: bool = True
    #: True when this layer's schedule is derived from a budget elsewhere (the
    #: gatewayd supervisor's cap is what the stub's reconnect budget is sized
    #: from) and so must not follow the shared ``agent.recovery_*`` knobs.
    pinned: bool = False

    def raw_backoff_secs(self, attempt: int) -> float:
        """Undithered ``base * 2**(attempt-1)`` capped at ``max_secs``.

        ``attempt`` is 1-based (the first retry follows the first failure); a
        value below 1 is treated as 1 so a caller that has not counted yet still
        gets the base delay.
        """
        exponent = max(0, min(int(attempt) - 1, _MAX_EXPONENT))
        return float(min(self.max_secs, self.base_secs * (2**exponent)))

    def backoff_secs(
        self,
        attempt: int,
        *,
        retry_after_secs: float | None = None,
        rng: random.Random | None = None,
    ) -> float:
        """The delay before retry number ``attempt`` at this layer.

        Equal jitter over the raw backoff, then floored by a server-stated
        ``retry_after_secs`` and finally capped at ``max_secs`` -- in that
        order, so the floor cannot lift the delay above the cap.
        """
        raw = self.raw_backoff_secs(attempt)
        delay = raw
        if self.jitter and raw > 0:
            draw = (rng or random).random()
            delay = raw / 2.0 + draw * (raw / 2.0)
        if retry_after_secs is not None and retry_after_secs > 0:
            delay = max(delay, float(retry_after_secs))
        return float(min(self.max_secs, delay))

    def should_escalate(self, attempts: int) -> bool:
        """True once ``attempts`` failures have been spent at this layer."""
        if self.max_attempts <= 0:
            return True
        return int(attempts) >= self.max_attempts


@dataclass(frozen=True)
class RecoveryPolicy:
    """The shared schedule plus its per-layer specialisations.

    ``base_secs`` / ``max_secs`` / ``jitter`` / ``cooldown_secs`` are the
    schedule every layer inherits; ``layers`` maps a layer name to the
    :class:`LayerPolicy` that already carries them. Build one with
    :func:`build_policy` (or :meth:`from_config`) rather than by hand so the
    layers and the shared schedule cannot disagree.
    """

    base_secs: float = DEFAULT_BACKOFF_BASE_SECS
    max_secs: float = DEFAULT_BACKOFF_MAX_SECS
    jitter: bool = True
    cooldown_secs: float = DEFAULT_COOLDOWN_SECS
    layers: dict[str, LayerPolicy] = field(default_factory=dict)

    def layer(self, name: str) -> LayerPolicy:
        try:
            return self.layers[name]
        except KeyError:
            raise KeyError(f"unknown recovery layer {name!r}") from None

    def backoff_secs(
        self,
        attempt: int,
        *,
        layer: str | None = None,
        retry_after_secs: float | None = None,
        rng: random.Random | None = None,
    ) -> float:
        """Delay for ``attempt`` at ``layer`` (or on the bare shared schedule)."""
        if layer is not None:
            return self.layer(layer).backoff_secs(
                attempt, retry_after_secs=retry_after_secs, rng=rng
            )
        bare = LayerPolicy(
            layer="",
            trigger="",
            max_attempts=1,
            base_secs=self.base_secs,
            max_secs=self.max_secs,
            jitter=self.jitter,
            cooldown_secs=self.cooldown_secs,
        )
        return bare.backoff_secs(attempt, retry_after_secs=retry_after_secs, rng=rng)

    def with_schedule(
        self,
        *,
        base_secs: float | None = None,
        max_secs: float | None = None,
        jitter: bool | None = None,
        cooldown_secs: float | None = None,
    ) -> "RecoveryPolicy":
        """A copy whose shared schedule (and every layer's) is overridden."""
        new_base = self.base_secs if base_secs is None else float(base_secs)
        new_max = self.max_secs if max_secs is None else float(max_secs)
        new_jitter = self.jitter if jitter is None else bool(jitter)
        new_cool = self.cooldown_secs if cooldown_secs is None else float(cooldown_secs)
        new_base = min(max(new_base, BACKOFF_BASE_MIN_SECS), BACKOFF_BASE_MAX_SECS)
        new_max = min(max(new_max, BACKOFF_MAX_MIN_SECS), BACKOFF_MAX_MAX_SECS)
        if new_max < new_base:
            new_max = new_base
        layers = {
            name: (
                lp
                if lp.pinned
                else replace(
                    lp,
                    base_secs=new_base,
                    max_secs=new_max,
                    jitter=new_jitter,
                    cooldown_secs=new_cool,
                )
            )
            for name, lp in self.layers.items()
        }
        return RecoveryPolicy(
            base_secs=new_base,
            max_secs=new_max,
            jitter=new_jitter,
            cooldown_secs=new_cool,
            layers=layers,
        )

    @classmethod
    def from_config(cls, cfg: Any, *, base: "RecoveryPolicy | None" = None) -> "RecoveryPolicy":
        """``base`` (or the default ladder policy) with the ``agent.recovery_*`` keys applied.

        Reads ``cfg.agent.recovery_backoff_base_secs`` and
        ``cfg.agent.recovery_backoff_max_secs`` through ``getattr`` so a config
        object that predates the keys -- or any duck-typed stand-in -- yields the
        defaults rather than an error. Recovery must never depend on config
        parsing succeeding.
        """
        from kiro_crew.recovery.ladder import LADDER  # leaf module; no cycle at import

        policy = base if base is not None else LADDER
        agent = getattr(cfg, "agent", None)
        base_secs = getattr(agent, "recovery_backoff_base_secs", None)
        max_secs = getattr(agent, "recovery_backoff_max_secs", None)
        if not isinstance(base_secs, (int, float)) or isinstance(base_secs, bool):
            base_secs = None
        if not isinstance(max_secs, (int, float)) or isinstance(max_secs, bool):
            max_secs = None
        if base_secs is None and max_secs is None:
            return policy
        return policy.with_schedule(base_secs=base_secs, max_secs=max_secs)


@dataclass
class _UnitState:
    attempts: int = 0
    last_failure_at: float = 0.0
    first_failure_at: float = 0.0


class RecoveryTracker:
    """Per-unit consecutive-failure counts with cooldown decay.

    The unit key is whatever the layer recovers: a slot key at L1, a backend
    key at L2, a runtime id at L3, the daemon at L4. Bounded to
    :data:`TRACKER_MAX_UNITS` entries (LRU eviction). Every method takes ``now``
    so tests drive it with an injected clock and production passes
    ``time.monotonic()``.
    """

    def __init__(self, *, cooldown_secs: float = DEFAULT_COOLDOWN_SECS) -> None:
        self._cooldown = float(cooldown_secs)
        self._units: OrderedDict[str, _UnitState] = OrderedDict()

    @property
    def cooldown_secs(self) -> float:
        return self._cooldown

    def _fresh(self, unit: str, now: float) -> _UnitState:
        state = self._units.get(unit)
        if state is None:
            return _UnitState()
        if state.attempts and now - state.last_failure_at >= self._cooldown:
            # The unit went a full cooldown without failing again: forget it.
            del self._units[unit]
            return _UnitState()
        return state

    def attempts(self, unit: str, now: float) -> int:
        """Consecutive failures recorded for ``unit`` that have not cooled down."""
        return self._fresh(unit, now).attempts

    def failing_since(self, unit: str, now: float) -> float | None:
        """``now`` of the first failure in the current run, or None when clean."""
        state = self._fresh(unit, now)
        return state.first_failure_at if state.attempts else None

    def record_failure(self, unit: str, now: float) -> int:
        """Count one more failure for ``unit``; returns the new attempt count."""
        state = self._fresh(unit, now)
        if state.attempts == 0:
            state.first_failure_at = now
        state.attempts += 1
        state.last_failure_at = now
        self._units[unit] = state
        self._units.move_to_end(unit)
        while len(self._units) > TRACKER_MAX_UNITS:
            self._units.popitem(last=False)
        return state.attempts

    def record_success(self, unit: str) -> None:
        """A unit recovered: its run of failures is over."""
        self._units.pop(unit, None)

    def forget(self, unit: str) -> None:
        self._units.pop(unit, None)

    def __len__(self) -> int:
        return len(self._units)


__all__ = [
    "BACKOFF_BASE_MAX_SECS",
    "BACKOFF_BASE_MIN_SECS",
    "BACKOFF_MAX_MAX_SECS",
    "BACKOFF_MAX_MIN_SECS",
    "DEFAULT_BACKOFF_BASE_SECS",
    "DEFAULT_BACKOFF_MAX_SECS",
    "DEFAULT_COOLDOWN_SECS",
    "TRACKER_MAX_UNITS",
    "LayerPolicy",
    "RecoveryPolicy",
    "RecoveryTracker",
]
