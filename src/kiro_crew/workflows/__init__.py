"""Dynamic workflows for KiroCrew — the FROZEN CONTRACT (no implementation yet).

This package defines the interface-first freeze for the dynamic-workflows feature:
the ``WorkflowContext`` Protocol (the ``ctx`` DSL surface an authored workflow
script calls) and the run-event types that drive the UI and future resume.

Everything else — the runner, the UI builtin app, schema enforcement, and the
KiroCrew-native primitive wrappers — is built in parallel against this contract.
Changing a signature or event shape here is a re-freeze and must update the
conformance test (GATE F2 in ``docs/system-specs/modules/workflow-gates.md``).

Spec: ``docs/system-specs/modules/workflows.md``.
Gate catalog: ``docs/system-specs/modules/workflow-gates.md``.

NOTE: This module is intentionally implementation-free. It declares Protocols and
typed event records only. ``import``-ing it must have no side effects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Protocol, Union, runtime_checkable

__all__ = [
    "WorkflowContext",
    "Budget",
    "CronPort",
    "MemoryPort",
    "LearnPort",
    "KnowledgePort",
    "BudgetExceeded",
    "WorkflowEvent",
    "EVENT_TYPES",
    "AgentResult",
    "Thunk",
]

# A thunk is a zero-arg callable returning an awaitable (used by parallel()).
Thunk = Callable[[], Any]
# What ctx.agent() resolves to: validated dict (schema=), free text, or None.
AgentResult = Union[str, dict, None]


class BudgetExceeded(Exception):  # noqa: N818 (frozen-contract name; see module spec)
    """Raised by ``ctx.agent()`` once cumulative token cost reaches ``budget.total``.

    This is the ONE error ``ctx.agent()`` is allowed to raise; all other agent
    failures resolve to ``None`` (see ``WorkflowContext.agent``).
    """


@runtime_checkable
class Budget(Protocol):
    """Token budget for a run. ``total`` is a HARD ceiling, not advisory."""

    total: Optional[int]  # None => no ceiling (remaining() is math.inf)

    def spent(self) -> int:
        """Output tokens spent so far this run."""

    def remaining(self) -> float:
        """``total - spent()``, or ``math.inf`` when ``total`` is None."""


@runtime_checkable
class CronPort(Protocol):
    """Self-scheduling surface (wraps ``apps.cron_sdk.CronSDK``)."""

    def ensure(self, name: str, *, cron_expr: str, workflow: str, **kw: Any) -> None:
        """Idempotently register a recurring workflow run."""

    def add(
        self,
        name: str,
        *,
        every_secs: Optional[int] = None,
        cron_expr: Optional[str] = None,
        workflow: str,
        **kw: Any,
    ) -> None:
        """Register a recurring workflow run (interval or cron)."""

    def remove(self, name: str) -> None:
        """Remove a previously registered job (owner-scoped)."""


@runtime_checkable
class MemoryPort(Protocol):
    """Cross-run key/value state (wraps ``MemoryStore``)."""

    def get(self, key: str, default: Any = None) -> Any:
        """Read a cross-run value, ``default`` if absent."""

    def set(self, key: str, value: Any) -> None:
        """Persist a JSON-serializable cross-run value."""


@runtime_checkable
class LearnPort(Protocol):
    """Persisted lessons (wraps ``learn.LessonStore``)."""

    def add(self, rule: str, scope: str = "workspace") -> None:
        """Persist a lesson the agent should apply in future runs."""


@runtime_checkable
class KnowledgePort(Protocol):
    """Searchable knowledge graph (wraps ``knowledge.KnowledgeStore``)."""

    async def search(self, query: str) -> list:
        """Hybrid (FTS5 + embedding) knowledge search; returns ranked hits."""


@runtime_checkable
class WorkflowContext(Protocol):
    """The ``ctx`` surface a workflow script calls. FROZEN — see module docstring.

    ``ctx.agent()`` spawns a SUBAGENT by default (isolated context, parallel,
    reusing the SubagentManager cap/queue). Pass ``session=<key>`` to run in a
    shared session instead (stateful chains).
    """

    # --- inputs / determinism (the only clock/seed in scope) ---
    args: dict
    now: str
    owner_dm: str

    # --- agent execution ---
    async def agent(
        self,
        prompt: str,
        *,
        label: Optional[str] = None,
        phase: Optional[str] = None,
        schema: Optional[dict] = None,
        model: Optional[str] = None,
        agent: Optional[str] = None,
        effort: Optional[str] = None,
        cwd: Optional[str] = None,
        session: Optional[str] = None,
        nudge: Optional[dict] = None,
    ) -> AgentResult:
        """Run one agent. Subagent by default; ``session=`` for in-session.

        With ``schema`` returns a validated dict (else free text). Returns
        ``None`` if skipped or the agent died; raises only ``BudgetExceeded``.
        """

    # --- scheduling primitives ---
    async def parallel(self, thunks: list[Thunk]) -> list:
        """Barrier: run all thunks concurrently; a failed thunk → ``None``."""

    async def pipeline(self, items: list, *stages: Callable) -> list:
        """Run each item through all stages independently (no inter-stage barrier)."""

    async def workflow(self, name: str, args: Optional[dict] = None) -> Any:
        """Run another workflow inline as a sub-step (one nesting level only)."""

    # --- progress / UI ---
    def phase(self, title: str) -> None:
        """Start a new progress phase; later ``agent()`` calls group under it."""

    def log(self, message: str) -> None:
        """Emit a narrator progress line to the UI."""

    budget: Budget

    # --- KiroCrew-native (M4); None when not permitted for this run ---
    cron: Optional[CronPort]
    memory: Optional[MemoryPort]
    learn: Optional[LearnPort]
    knowledge: Optional[KnowledgePort]

    def nudge(self, *, idle_secs: int, message: str, max_cycles: int = 0) -> None:
        """Arm a reactive self-prompt if the run idles past ``idle_secs``."""

    async def approve(self, prompt: str) -> bool:
        """Human-in-the-loop gate; resolves to the human's decision."""

    async def send_slack(self, target: str, text: str) -> None:
        """Deliver a message to a Slack target."""

    async def send_message(self, channel: str, text: str) -> None:
        """Deliver a message to a dashboard/session channel."""


# --- Run event stream (JSON only — NEVER pickle, ARCC BSC12) ---

#: The frozen set of event ``type`` values. The runner emits exactly these.
EVENT_TYPES = (
    "run_started",
    "phase_started",
    "agent_started",
    "agent_progress",
    "agent_finished",
    "log",
    "budget_update",
    "approval_requested",
    "run_finished",
    "run_failed",
    "run_cancelled",
)


@dataclass
class WorkflowEvent:
    """One run event. Serialized to/from JSON only (BSC12 — no pickle).

    ``data`` carries the event-specific fields documented in
    ``docs/system-specs/modules/workflows.md`` (run event stream table).
    """

    run_id: str
    seq: int
    ts: str
    type: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        if self.type not in EVENT_TYPES:
            raise ValueError(f"unknown workflow event type: {self.type!r}")
        return {
            "run_id": self.run_id,
            "seq": self.seq,
            "ts": self.ts,
            "type": self.type,
            "data": self.data,
        }

    @classmethod
    def from_json(cls, obj: dict[str, Any]) -> "WorkflowEvent":
        return cls(
            run_id=obj["run_id"],
            seq=obj["seq"],
            ts=obj["ts"],
            type=obj["type"],
            data=obj.get("data", {}),
        )
