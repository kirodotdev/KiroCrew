"""Learn a bounded subagent timeout from observed run duration."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import config_dir
from kiro_crew.hooks import FileTooLargeError, safe_read_file_bytes_nolink

logger = logging.getLogger(__name__)

DEFAULT_ADAPTIVE_TIMEOUT_MAX_SECS = 7200
_TIMEOUT_GROWTH_SECS = 1800
_TIMEOUT_GROWTH_FACTOR = 1.5
_TIMEOUT_ROUND_SECS = 300
_NEAR_LIMIT_RATIO = 0.8
_STATE_MAX_BYTES = 4096


@dataclass(frozen=True)
class TimeoutAdjustment:
    timeout_secs: int
    reason: str = ""

    @property
    def changed(self) -> bool:
        return bool(self.reason)


def _timeout_state_path() -> Path:
    return config_dir() / "subagents" / "timeout_state.json"


def _next_timeout(current: int, ceiling: int) -> int:
    scaled = int(
        (current * _TIMEOUT_GROWTH_FACTOR + _TIMEOUT_ROUND_SECS - 1) // _TIMEOUT_ROUND_SECS
    )
    scaled *= _TIMEOUT_ROUND_SECS
    return min(ceiling, max(current + _TIMEOUT_GROWTH_SECS, scaled))


def read_learned_timeout() -> int | None:
    """Read the bounded learned level without following links or aliases."""
    path = _timeout_state_path()
    try:
        raw = safe_read_file_bytes_nolink(
            str(path),
            within_root=str(path.parent),
            max_bytes=_STATE_MAX_BYTES,
        )
    except (FileTooLargeError, OSError, ValueError):
        return None
    if raw is None:
        return None
    try:
        record = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    value = record.get("timeout_secs") if isinstance(record, dict) else None
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def write_learned_timeout(timeout_secs: int, reason: str) -> None:
    """Replace the learned state without following a planted destination link."""
    record = {
        "timeout_secs": timeout_secs,
        "reason": reason,
        "ts": int(time.time()),
    }
    try:
        atomic_write(
            _timeout_state_path(),
            json.dumps(record, ensure_ascii=False) + "\n",
            restrict_to_owner=True,
        )
    except (OSError, ValueError):
        logger.debug("Failed to persist learned subagent timeout", exc_info=True)


class AdaptiveTimeoutPolicy:
    """Raise future run deadlines after timeouts or near-limit completions."""

    def __init__(self, base_secs: int, max_secs: int, *, enabled: bool) -> None:
        self.enabled = enabled
        self.base_secs = max(1, base_secs)
        self.max_secs = max(self.base_secs, max_secs)
        self.current_secs = self.base_secs

    def restore(self, learned_secs: int | None) -> int:
        if self.enabled and learned_secs is not None:
            self.current_secs = max(
                self.current_secs,
                self.base_secs,
                min(self.max_secs, learned_secs),
            )
        return self.current_secs

    def observe(
        self,
        deadline_secs: int,
        elapsed_secs: float,
        *,
        completed: bool,
    ) -> TimeoutAdjustment:
        """Record one terminal run and return any newly earned future deadline."""
        if not self.enabled or deadline_secs < self.current_secs:
            return TimeoutAdjustment(self.current_secs)
        reason = "near_limit_completion" if completed else "timeout"
        if completed and elapsed_secs < deadline_secs * _NEAR_LIMIT_RATIO:
            return TimeoutAdjustment(self.current_secs)
        raised = _next_timeout(self.current_secs, self.max_secs)
        if raised <= self.current_secs:
            return TimeoutAdjustment(self.current_secs)
        self.current_secs = raised
        logger.warning(
            "Adaptive subagent timeout raised from %ds to %ds after %s",
            deadline_secs,
            raised,
            reason,
        )
        return TimeoutAdjustment(raised, reason)
