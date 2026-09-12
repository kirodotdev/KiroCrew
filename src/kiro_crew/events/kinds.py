"""Typed event set for the structured lifecycle log.

Deliberately minimal and consumer-driven: every kind here is CONSTRUCTED by a
shipped caller -- today the backfill validator, proving schema fit against the
real stores. That rule holds while the validator exists: it is disposable by
contract, so a kind that outlives it must have an emitter constructing it. A kind
that arrives with no constructor does not belong here.

Facts that need ORDER, threading or citation are not this track's to name. The
decision of record is ``docs/request-for-change/rfc-append-only-ledger.md``: they
go to the per-unit append-only ledger, whose writer assigns a per-unit ``seq``,
and turn boundaries are the first of them -- ``kiro_crew.session_ledger_emit``
writes them there. So a kind for such a fact is not merely un-emitted here, it
belongs to the other stream. What remains for this track is unsequenced facts
whose emitters land with them; additive-only evolution makes that free, and
publishing a vocabulary nothing writes would not.

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
