"""Typed event set for the structured lifecycle log.

Deliberately minimal and consumer-driven: every kind here is CONSTRUCTED by a
shipped caller -- today the backfill validator, proving schema fit against the
real stores. That rule holds only while the validator exists: it is disposable
by contract, so when the first emit sites land and it is deleted, the SAME rule
requires those emitters to be the constructors. A kind that arrives at that
point with no emitter constructing it does not belong here. Kinds for facts no
one writes yet (turn boundaries, cron fires, nudge cycles, workflow phases)
therefore land WITH their emitters -- additive-only evolution makes that free,
and shipping the names early would publish a vocabulary nothing writes.

All fields beyond the base envelope are optional wherever the historical stores
cannot guarantee them. The validator's job is to measure how much of the real
data actually fits, which is why its report carries per-field fill counts: a
field no store ever populates is a field this schema has not earned.
"""

from __future__ import annotations

from dataclasses import dataclass

from kiro_crew.events.base import Event, register

# ── session domain ────────────────────────────────────────────────────────


@register
@dataclass(frozen=True)
class SessionMessage(Event):
    """One conversation row (user or assistant) appended to a session.

    No ``agent`` field: the transcript store does not record one. Validating
    against a live data home found it populated on 0 of 56,384 real rows, so it
    would be a field the schema had not earned. The store DOES carry
    ``source_thread`` / ``source_user`` / ``meta``, which a later revision can
    add additively once something consumes them.
    """

    KIND = "session/message"

    role: str = ""
    content_chars: int = 0


# ── turn domain ───────────────────────────────────────────────────────────


@register
@dataclass(frozen=True)
class TurnUsage(Event):
    """Per-turn usage snapshot (mirrors one usage-shard row, by reference)."""

    KIND = "turn/usage"

    model: str | None = None
    provider: str | None = None
    credits: float | None = None
    cost: float | None = None
    turns: int | None = None
    duration_ms: int | None = None


# ── subagent domain ───────────────────────────────────────────────────────


@register
@dataclass(frozen=True)
class SubagentSpawned(Event):
    KIND = "subagent/spawned"

    task_preview: str | None = None
    pid: int | None = None
    parent_key: str | None = None


@register
@dataclass(frozen=True)
class SubagentCompleted(Event):
    KIND = "subagent/completed"

    turns: int | None = None
    result_bytes: int | None = None


@register
@dataclass(frozen=True)
class SubagentFailed(Event):
    KIND = "subagent/failed"

    reason: str | None = None


# ── cron domain ───────────────────────────────────────────────────────────


@register
@dataclass(frozen=True)
class CronRegistered(Event):
    """A job present in the cron store (registration/backfill snapshot)."""

    KIND = "cron/registered"

    name: str | None = None
    schedule: str | None = None
    paused: bool | None = None
    kind_label: str | None = None


# ── autonudge domain ──────────────────────────────────────────────────────


@register
@dataclass(frozen=True)
class AutonudgeArmed(Event):
    KIND = "autonudge/armed"

    interval_secs: int | None = None
    max_cycles: int | None = None


__all__ = [
    "SessionMessage",
    "TurnUsage",
    "SubagentSpawned",
    "SubagentCompleted",
    "SubagentFailed",
    "CronRegistered",
    "AutonudgeArmed",
]
