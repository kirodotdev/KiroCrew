"""Learn a bounded subagent timeout from observed run duration."""

from __future__ import annotations

import json
import logging
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from kiro_crew import hooks, platform_compat
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import config_dir

logger = logging.getLogger(__name__)

DEFAULT_ADAPTIVE_TIMEOUT_MAX_SECS = 21600
_TIMEOUT_GROWTH_SECS = 1800
_TIMEOUT_GROWTH_FACTOR = 1.5
_TIMEOUT_ROUND_SECS = 300
_NEAR_LIMIT_RATIO = 0.8
_TIMEOUT_STATE_DIR_LEAF = "member-memory-bindings"
_TIMEOUT_STATE_FILE = "subagent-timeout.json"
_STATE_MAX_BYTES = 4096


@dataclass(frozen=True)
class TimeoutAdjustment:
    timeout_secs: int
    reason: str = ""

    @property
    def changed(self) -> bool:
        return bool(self.reason)


def _timeout_state_path() -> Path:
    return config_dir() / _TIMEOUT_STATE_DIR_LEAF / _TIMEOUT_STATE_FILE


def _read_timeout_state_bytes(path: Path) -> bytes | None:
    """Read the gateway-owned state without opening an agent-selected path."""
    root = path.parent.parent
    try:
        root_real = os.path.realpath(os.fspath(root))
        relative = path.relative_to(root)
        expected_real = os.path.join(root_real, *relative.parts)
    except (OSError, ValueError):
        return None

    try:
        fd = platform_compat.open_file_no_reparse(path, nonblocking=True)
    except OSError:
        return None
    try:
        opened = os.fstat(fd)
        if opened.st_nlink > 1 or not stat.S_ISREG(opened.st_mode):
            return None
        opened_real = hooks._fd_real_path(fd)
        if opened_real is None:
            return None
        opened_norm = os.path.normcase(os.path.normpath(opened_real))
        expected_norm = os.path.normcase(os.path.normpath(expected_real))
        root_norm = os.path.normcase(os.path.normpath(root_real))
        try:
            contained = os.path.commonpath([opened_norm, root_norm]) == root_norm
        except ValueError:
            contained = False
        if not contained or opened_norm != expected_norm:
            return None
        with os.fdopen(fd, "rb") as fh:
            data = fh.read(_STATE_MAX_BYTES + 1)
        fd = -1
        return data if len(data) <= _STATE_MAX_BYTES else None
    except OSError:
        return None
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass


def _next_timeout(current: int, ceiling: int) -> int:
    scaled = int(
        (current * _TIMEOUT_GROWTH_FACTOR + _TIMEOUT_ROUND_SECS - 1) // _TIMEOUT_ROUND_SECS
    )
    scaled *= _TIMEOUT_ROUND_SECS
    return min(ceiling, max(current + _TIMEOUT_GROWTH_SECS, scaled))


def read_learned_timeout() -> int | None:
    """Read the bounded level from one gateway-only atomic record."""
    raw = _read_timeout_state_bytes(_timeout_state_path())
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


def write_learned_timeout(timeout_secs: int) -> None:
    """Atomically replace the protected learned-timeout record."""
    payload = json.dumps({"timeout_secs": timeout_secs}, ensure_ascii=False) + "\n"
    try:
        atomic_write(
            _timeout_state_path(),
            payload,
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

    def reconfigure(self, base_secs: int, max_secs: int, *, enabled: bool) -> int:
        """Apply live settings without discarding an enabled learned level."""
        self.enabled = enabled
        self.base_secs = max(1, base_secs)
        self.max_secs = max(self.base_secs, max_secs)
        if enabled:
            self.current_secs = max(
                self.base_secs,
                min(self.current_secs, self.max_secs),
            )
        else:
            self.current_secs = self.base_secs
        return self.current_secs

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
