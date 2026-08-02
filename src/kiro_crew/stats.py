"""Runtime statistics — thread-safe counters for messages, tools, sessions, subagents."""

from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger(__name__)


class Stats:
    """Singleton collecting runtime counters with lock-guarded increments."""

    _instance: Stats | None = None
    _lock = threading.Lock()

    def __new__(cls) -> Stats:
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    # Build into a local and FULLY initialize it before
                    # publishing to ``cls._instance``. Publishing first would
                    # let a second thread on the lock-free fast path above
                    # observe a non-None but half-built instance (no ``_mu`` /
                    # ``_c`` yet) and raise AttributeError on the next
                    # ``.inc()`` / ``.snapshot()``.
                    inst = super().__new__(cls)
                    inst._init_counters()
                    cls._instance = inst
        return cls._instance

    def _init_counters(self) -> None:
        self._mu = threading.Lock()
        self._start_time = time.monotonic()
        self._c: dict[str, int] = {
            "messages_received": 0,
            "messages_success": 0,
            "messages_failed": 0,
            "tool_approvals": 0,
            "tool_denials": 0,
            "tool_auto_approved": 0,
            "timeouts": 0,
            "sessions_created": 0,
            "sessions_cleaned": 0,
            "subagents_spawned": 0,
            "subagents_completed": 0,
            "subagents_failed": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_tokens": 0,
            "cache_read_tokens": 0,
            "total_turns": 0,
            "total_duration_ms": 0,
        }
        self._cost_usd: float = 0.0

    # -- mutators --

    def inc(self, key: str, n: int = 1) -> None:
        """Increment counter *key* by *n*."""
        with self._mu:
            self._c[key] = self._c.get(key, 0) + n

    def inc_message_received(self) -> None:
        self.inc("messages_received")

    def inc_message_success(self) -> None:
        self.inc("messages_success")

    def inc_message_failed(self) -> None:
        self.inc("messages_failed")

    def inc_tool_approval(self) -> None:
        self.inc("tool_approvals")

    def inc_tool_denial(self) -> None:
        self.inc("tool_denials")

    def inc_tool_auto_approved(self) -> None:
        self.inc("tool_auto_approved")

    def inc_input_tokens(self, n: int) -> None:
        self.inc("input_tokens", n)

    def inc_output_tokens(self, n: int) -> None:
        self.inc("output_tokens", n)

    def inc_cache_creation_tokens(self, n: int) -> None:
        self.inc("cache_creation_tokens", n)

    def inc_cache_read_tokens(self, n: int) -> None:
        self.inc("cache_read_tokens", n)

    def inc_cost_usd(self, amount: float) -> None:
        with self._mu:
            self._cost_usd += amount

    def inc_turns(self, n: int) -> None:
        self.inc("total_turns", n)

    def inc_duration_ms(self, n: int) -> None:
        self.inc("total_duration_ms", n)

    def get_cost_usd(self) -> float:
        with self._mu:
            return self._cost_usd

    def inc_timeout(self) -> None:
        self.inc("timeouts")

    def inc_session_created(self) -> None:
        self.inc("sessions_created")

    def inc_session_cleaned(self) -> None:
        self.inc("sessions_cleaned")

    def inc_subagent_spawned(self) -> None:
        self.inc("subagents_spawned")

    def inc_subagent_completed(self) -> None:
        self.inc("subagents_completed")

    def inc_subagent_failed(self) -> None:
        self.inc("subagents_failed")

    # -- queries --

    def uptime_str(self) -> str:
        """Human-readable uptime."""
        secs = round(time.monotonic() - self._start_time)
        h, rem = divmod(secs, 3600)
        d, h = divmod(h, 24)
        m, _s = divmod(rem, 60)
        parts: list[str] = []
        if d:
            parts.append(f"{d}d")
        parts.append(f"{h}h")
        parts.append(f"{m}m")
        return " ".join(parts)

    def snapshot(self) -> dict[str, int]:
        """Return a copy of all counters."""
        with self._mu:
            return dict(self._c)

    def summary(self) -> str:
        """One-line summary for Slack status replies."""
        s = self.snapshot()
        return (
            f"uptime {self.uptime_str()} · "
            f"msgs {s['messages_received']} "
            f"(ok {s['messages_success']} / fail {s['messages_failed']}) · "
            f"tools approved {s['tool_approvals']} denied {s['tool_denials']} "
            f"auto {s['tool_auto_approved']} · "
            f"timeouts {s['timeouts']} · "
            f"sessions {s['sessions_created']}/{s['sessions_cleaned']} · "
            f"subagents {s['subagents_spawned']}"
        )

    def daily_report(self) -> str:
        """Multi-line report suitable for a daily digest."""
        s = self.snapshot()
        total = s["messages_received"]
        if total == 0:
            health = "🔇 no messages"
        else:
            rate = s["messages_success"] / total * 100
            if rate >= 90:
                health = f"🟢 healthy ({rate:.0f}%)"
            elif rate >= 70:
                health = f"🟡 degraded ({rate:.0f}%)"
            else:
                health = f"🔴 critical ({rate:.0f}%)"
        return (
            f"📊 *KiroCrew Daily Report*\n"
            f"Health: {health}\n"
            f"Uptime: {self.uptime_str()}\n"
            f"Messages: {s['messages_received']} received, "
            f"{s['messages_success']} ok, {s['messages_failed']} failed\n"
            f"Tools: {s['tool_approvals']} approved, {s['tool_denials']} denied, "
            f"{s['tool_auto_approved']} auto-approved\n"
            f"Timeouts: {s['timeouts']}\n"
            f"Sessions: {s['sessions_created']} created, {s['sessions_cleaned']} cleaned\n"
            f"Subagents: {s['subagents_spawned']} spawned, "
            f"{s['subagents_completed']} completed, {s['subagents_failed']} failed"
        )

    def reset(self) -> None:
        """Zero all counters and restart uptime clock."""
        with self._mu:
            for k in self._c:
                self._c[k] = 0
            self._start_time = time.monotonic()
