"""Runtime-learned routing tiers for models that publish no curated tier.

Why this module exists
----------------------
A model's routing tier (fast / balanced / strong) must come from a trustworthy
fact, never from the words in its id. Curated catalog metadata and an operator's
manual ``tier`` are trustworthy. A relay/New-API model that only advertises an
id is not: calling it "opus" does not make it strong.

So such a model starts in ``balanced`` (see ``mode_router.model_tiers``) and may
*earn* a fast routing role from real, content-free runtime evidence that the
adaptive layer already collects (relative speed + reliability). Runtime speed
cannot establish reasoning quality, so it must never manufacture a strong role.
This module is
the small, auditable ledger that records a learned tier and the strict gates
that may move it.

Gates (all required)
---------------------
* High confidence only. The adaptive snapshot must report ``confidence == "high"``
  for the model (i.e. enough recent samples). One lucky/unlucky turn never moves
  a tier.
* One step at a time. Uncurated models move only between balanced and fast.
  A strong role requires trusted quality evidence, not latency telemetry.
* Audit trail. Every change appends a record (model, from, to, evidence, time)
  so a human can see exactly why a model moved.
* Curated models are never learned. If a model already has an authoritative tier
  (manual ``tier`` or curated ``routing`` metadata), this ledger leaves it alone.

This module deliberately learns ONLY the tier. It does not invent quality/cost
"facts" and it does not touch the raw telemetry or the scoring weights. It is
the Dyna half of "Tiga reads the tier, Dyna reads the outcome": the outcome,
once trustworthy, is allowed to set a tier that Tiga can then read.
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .paths import app_home
from .state_io import _file_transaction_lock

_TIERS_ORDER = ("fast", "balanced", "strong")
_VALID_TIERS = frozenset(_TIERS_ORDER)

# How many audit records to retain. This is a bounded diagnostic log, not
# long-term analytics: a model that keeps moving should not grow the file
# without end.
_MAX_AUDIT_RECORDS = 500

_INSTANCE_LOCKS: dict[str, threading.Lock] = {}
_INSTANCE_LOCKS_GUARD = threading.Lock()


def _step_toward(current: str, direction: int) -> str:
    """Move one single step along fast <-> balanced <-> strong.

    direction > 0 moves toward 'strong', direction < 0 toward 'fast'. The move
    is clamped at the ends, so a change that cannot step is a no-op (reported by
    the caller, which then records nothing).
    """
    try:
        index = _TIERS_ORDER.index(current)
    except ValueError:
        index = 1  # unknown -> treat as balanced
    nxt = max(0, min(len(_TIERS_ORDER) - 1, index + (1 if direction > 0 else -1)))
    return _TIERS_ORDER[nxt]


@dataclass(frozen=True)
class LearnedTier:
    tier: str
    updated_at: float
    samples: int
    reason: str


class LearnedTierStore:
    """Read/write the learned-tier map plus a bounded audit trail.

    File shape (JSON):
        {
          "tiers": {"<provider_id>\\t<model_id>": {"tier","updated_at","samples","reason"}},
          "audit": [ {"timestamp","provider_id","model_id","from","to","samples","reason"} ]
        }

    The ledger is keyed by provider_id + model_id so the SAME model id served by
    two different providers (an official provider and a relay) never share a
    learned tier. That mirrors the R24 identity rule (provider instance + model
    id), so learning cannot leak across providers.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (app_home() / "data" / "routing" / "learned_tiers.json")
        try:
            key = str(self.path.resolve())
        except OSError:
            key = str(self.path)
        with _INSTANCE_LOCKS_GUARD:
            self._lock = _INSTANCE_LOCKS.setdefault(key, threading.Lock())

    @staticmethod
    def _key(provider_id: str, model_id: str) -> str:
        return f"{str(provider_id)}\t{str(model_id)}"

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"tiers": {}, "audit": []}
        try:
            with self.path.open("r", encoding="utf-8-sig") as handle:
                raw = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return {"tiers": {}, "audit": []}
        if not isinstance(raw, dict):
            return {"tiers": {}, "audit": []}
        tiers = raw.get("tiers") if isinstance(raw.get("tiers"), dict) else {}
        audit = raw.get("audit") if isinstance(raw.get("audit"), list) else []
        return {"tiers": tiers, "audit": audit}

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f"{self.path.name}.tmp-{os.getpid()}-{threading.get_ident()}")
        try:
            with tmp.open("w", encoding="utf-8", newline="\n") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
            tmp.replace(self.path)
        finally:
            tmp.unlink(missing_ok=True)

    def learned_tier(self, provider_id: str, model_id: str) -> str:
        """Return the learned tier for a model, or '' when none is recorded."""
        data = self._read()
        row = data["tiers"].get(self._key(provider_id, model_id))
        if isinstance(row, dict):
            tier = str(row.get("tier") or "").strip().lower()
            if tier in _VALID_TIERS:
                return tier
        return ""

    def all_learned(self) -> dict[tuple[str, str], LearnedTier]:
        data = self._read()
        out: dict[tuple[str, str], LearnedTier] = {}
        for key, row in data["tiers"].items():
            if not isinstance(row, dict):
                continue
            tier = str(row.get("tier") or "").strip().lower()
            if tier not in _VALID_TIERS:
                continue
            provider, _, model = str(key).partition("\t")
            out[(provider, model)] = LearnedTier(
                tier=tier,
                updated_at=float(row.get("updated_at") or 0.0),
                samples=int(row.get("samples") or 0),
                reason=str(row.get("reason") or ""),
            )
        return out

    def recent_audit(self, limit: int = 50) -> list[dict[str, Any]]:
        data = self._read()
        audit = data["audit"]
        return list(audit[-max(0, int(limit)):])

    def _apply_change(
        self, provider_id: str, model_id: str, new_tier: str, *, samples: int, reason: str,
    ) -> dict[str, Any]:
        with self._lock, _file_transaction_lock(self.path):
            data = self._read()
            key = self._key(provider_id, model_id)
            previous = data["tiers"].get(key)
            from_tier = ""
            if isinstance(previous, dict):
                from_tier = str(previous.get("tier") or "").strip().lower()
            data["tiers"][key] = {
                "tier": new_tier,
                "updated_at": time.time(),
                "samples": int(samples),
                "reason": reason,
            }
            record = {
                "timestamp": time.time(),
                "provider_id": str(provider_id),
                "model_id": str(model_id),
                "from": from_tier or "balanced",
                "to": new_tier,
                "samples": int(samples),
                "reason": reason,
            }
            data["audit"].append(record)
            if len(data["audit"]) > _MAX_AUDIT_RECORDS:
                data["audit"] = data["audit"][-_MAX_AUDIT_RECORDS:]
            self._write(data)
            return record


@dataclass(frozen=True)
class TierLearningProfile:
    """Thresholds for an auto tier move. Deliberately conservative."""
    # Relative runtime speed (0..1, peer-ranked) above which a model is "fast".
    fast_speed: float = 0.72
    # ...and below which a model is "slow".
    slow_speed: float = 0.30
    # Reliability (0..1) above which a model is "reliable".
    reliable: float = 0.90
    # ...and below which it is "unreliable".
    unreliable: float = 0.80


def evaluate_tier_change(
    *,
    current_effective_tier: str,
    runtime_speed: float | None,
    reliability: float,
    confidence: str,
    samples: int,
    profile: TierLearningProfile | None = None,
) -> tuple[int, str]:
    """Decide a single-step tier move from runtime evidence.

    Returns ``(direction, reason)`` where +1 moves toward balanced/strong,
    -1 toward fast, and 0 leaves the role unchanged. Only high-confidence
    observations can move a role. Runtime evidence never promotes to strong.

    This never reads a model name. It consumes only peer-relative speed and
    observed reliability, exactly the facts adaptive_routing can prove.
    """
    profile = profile or TierLearningProfile()
    if str(confidence).strip().lower() != "high":
        return 0, ""
    speed = float(runtime_speed) if runtime_speed is not None else 0.5

    reliable = reliability >= profile.reliable
    unreliable = reliability < profile.unreliable
    fast = speed >= profile.fast_speed
    slow = speed <= profile.slow_speed

    # Runtime data can prove speed and request reliability, not reasoning
    # quality. Therefore an unknown model can earn a fast role, but never a
    # strong role. Old learned "strong" entries must decay to balanced.
    if current_effective_tier == "strong":
        return -1, "legacy learned strong role has no quality evidence"
    if current_effective_tier == "balanced" and fast and reliable:
        return -1, f"fast-role evidence: speed={speed:.2f} reliability={reliability:.2f} over {samples} samples"
    if current_effective_tier == "fast" and (slow or unreliable):
        return 1, f"fast-role revoked: speed={speed:.2f} reliability={reliability:.2f} over {samples} samples"
    return 0, ""



# --- Throttled learning entry point ------------------------------------------
#
# Routing is a hot path. Writing the ledger on every single route call would add
# needless disk I/O (the project explicitly avoids unnecessary runtime
# overhead). So the observer below does cheap in-memory work on every call and
# only ever touches disk when BOTH a high-confidence move is warranted AND this
# model has not been written recently. A model that is stable writes nothing.

# Minimum seconds between two disk writes for the SAME model. A tier that is
# genuinely moving still moves; this only stops a flapping model from rewriting
# the ledger on back-to-back turns.
_MIN_WRITE_INTERVAL_SECONDS = 600.0

_LAST_WRITE: dict[tuple[str, str], float] = {}
_LAST_WRITE_GUARD = threading.Lock()


def observe_and_maybe_learn(
    *,
    provider_id: str,
    model_id: str,
    effective_tier: str,
    has_curated_tier: bool,
    runtime_speed: float | None,
    reliability: float,
    confidence: str,
    samples: int,
    store: "LearnedTierStore | None" = None,
    profile: TierLearningProfile | None = None,
    now: float | None = None,
) -> dict[str, Any] | None:
    """Maybe move a model's learned tier one step, honouring every gate.

    Returns the audit record when a change was written, else None. Safe to call
    on the routing hot path: it fails closed on any error and is throttled so a
    stable or curated model never hits disk.

    * ``has_curated_tier`` True means the model already has an authoritative
      tier (manual or curated metadata). Learning is skipped entirely: curated
      facts are never overwritten by runtime guessing.
    * ``effective_tier`` is the tier the router is currently using for the
      model (which, for a learnable model, is either 'balanced' or its own
      previously-learned tier). Moves are one step from here.
    """
    try:
        if has_curated_tier:
            return None
        now = float(now if now is not None else time.time())
        key = (str(provider_id), str(model_id))

        direction, reason = evaluate_tier_change(
            current_effective_tier=str(effective_tier or "balanced"),
            runtime_speed=runtime_speed,
            reliability=reliability,
            confidence=confidence,
            samples=samples,
            profile=profile,
        )
        if direction == 0:
            return None

        # Throttle disk writes per model. Checked only AFTER a real move is
        # warranted, so a stable model costs nothing here.
        with _LAST_WRITE_GUARD:
            last = _LAST_WRITE.get(key, 0.0)
            if now - last < _MIN_WRITE_INTERVAL_SECONDS:
                return None
            _LAST_WRITE[key] = now

        target = _step_toward(str(effective_tier or "balanced"), direction)
        if target == str(effective_tier or "balanced"):
            return None  # already at the end stop; nothing to move

        store = store or LearnedTierStore()
        return store._apply_change(
            provider_id, model_id, target, samples=samples, reason=reason,
        )
    except Exception:
        # Learning must never break routing.
        return None
