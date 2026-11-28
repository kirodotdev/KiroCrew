"""Provider-neutral subagent runtime primitives for the BYO harness.

The runtime deliberately separates *planning* from *execution*.  Policy packs
produce :class:`ExecutionPlan` objects; the Turn Engine may then execute one or
more :class:`AgentTask` objects without knowing product-facing Type names.

0.1.7 hotfix5 adds the first real child-agent semantics:

* stable ``task_id`` identities across a session process;
* isolated child transcript snapshots (never merged wholesale into the parent);
* per-task model/tool/authority contracts;
* bounded parallel execution for read-only workers;
* cancellation and mailbox-based continuation;
* sticky concrete model pins for a child once it starts.

Durable persistence across a BYO process restart intentionally remains a later
``Run`` capability.  The public contracts below are designed so that adding
persistence does not require another Turn Engine rewrite.
"""
from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

_AGENT_STATES = frozenset({
    "planned", "running", "completed", "failed", "cancelled", "skipped", "paused"
})
_TERMINAL_STATES = frozenset({"completed", "failed", "cancelled", "skipped"})


@dataclass(frozen=True)
class TaskProfile:
    """Multi-dimensional task shape used by orchestration policy."""

    complexity: float
    breadth: float
    parallelizability: float
    risk: float
    tool_intensity: float
    write_intensity: float
    context_demand: float
    verification_value: float
    latency_sensitivity: float
    task_class: str = "casual"

    def public_dict(self) -> dict[str, Any]:
        return {
            "complexity": round(self.complexity, 4),
            "breadth": round(self.breadth, 4),
            "parallelizability": round(self.parallelizability, 4),
            "risk": round(self.risk, 4),
            "tool_intensity": round(self.tool_intensity, 4),
            "write_intensity": round(self.write_intensity, 4),
            "context_demand": round(self.context_demand, 4),
            "verification_value": round(self.verification_value, 4),
            "latency_sensitivity": round(self.latency_sensitivity, 4),
            "task_class": self.task_class,
        }

    @classmethod
    def from_signals(cls, signals: Any) -> "TaskProfile":
        def value(name: str, fallback: float) -> float:
            raw = getattr(signals, name, fallback)
            try:
                return max(0.0, min(1.0, float(raw)))
            except (TypeError, ValueError):
                return fallback

        complexity = value("complexity", 0.0)
        return cls(
            complexity=complexity,
            breadth=value("breadth", complexity * 0.45),
            parallelizability=value("parallelizability", complexity * 0.35),
            risk=value("risk", complexity * 0.35),
            tool_intensity=value("tool_intensity", 0.75 if getattr(signals, "tools_required", False) else 0.0),
            write_intensity=value("write_intensity", 0.0),
            context_demand=value("context_demand", min(1.0, float(getattr(signals, "estimated_input_tokens", 0) or 0) / 100000.0)),
            verification_value=value("verification_value", complexity * 0.55),
            latency_sensitivity=value("latency_sensitivity", max(0.0, 0.65 - complexity * 0.45)),
            task_class=str(getattr(signals, "task_profile", "casual") or "casual"),
        )


@dataclass(frozen=True)
class AgentSpec:
    """Execution contract for one logical agent in a plan.

    ``role`` is the semantic family (``lead``, ``worker``, ``review``). ``name``
    is the unique plan-local identity, allowing several workers without making
    the Turn Engine invent role names such as ``worker_2``.
    """

    role: str
    objective: str
    model_policy: str = "primary"
    tier_pool: str = "any"
    tool_policy: str = "inherit"
    authority: str = "assist"
    reasoning_intent: str = "inherit"
    max_turns: int = 0
    timeout_seconds: float = 0.0
    sticky_model: bool = True
    required: bool = False
    name: str = ""
    context_policy: str = "isolated_snapshot"
    backend: str = "model"
    backend_id: str = ""
    workspace_policy: str = "shared"

    @property
    def agent_name(self) -> str:
        return self.name or self.role

    def public_dict(self) -> dict[str, Any]:
        return {
            "name": self.agent_name,
            "role": self.role,
            "objective": self.objective,
            "model_policy": self.model_policy,
            "tier_pool": self.tier_pool,
            "tool_policy": self.tool_policy,
            "authority": self.authority,
            "reasoning_intent": self.reasoning_intent,
            "max_turns": self.max_turns,
            "timeout_seconds": self.timeout_seconds,
            "sticky_model": self.sticky_model,
            "required": self.required,
            "context_policy": self.context_policy,
            "backend": self.backend,
            "backend_id": self.backend_id,
            "workspace_policy": self.workspace_policy,
        }


@dataclass(frozen=True)
class AgentResult:
    """Structured hand-back contract; child transcripts stay isolated."""

    status: str
    summary: str = ""
    findings: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    unresolved: tuple[str, ...] = ()
    provider_id: str = ""
    model_id: str = ""
    duration_ms: float = 0.0
    usage: dict[str, Any] = field(default_factory=dict)

    def public_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "summary": self.summary,
            "findings": list(self.findings),
            "evidence": list(self.evidence),
            "unresolved": list(self.unresolved),
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "duration_ms": round(float(self.duration_ms), 3),
            "usage": dict(self.usage),
        }


@dataclass
class AgentTask:
    task_id: str
    plan_id: str
    session_id: str
    spec: AgentSpec
    parent_task_id: str = ""
    status: str = "planned"
    created_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    completed_at: float = 0.0
    provider_id: str = ""
    model_id: str = ""
    result: AgentResult | None = None
    error: str = ""
    generation: int = 0
    continuation_count: int = 0
    depth: int = 0
    workspace_id: str = ""
    workspace_path: str = ""
    workspace_kind: str = ""
    workspace_state: str = ""
    # Private mutable runtime state. It is intentionally absent from public_dict.
    context_messages: list[dict[str, Any]] = field(default_factory=list, repr=False)
    inbox: deque[str] = field(default_factory=deque, repr=False)
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    # Serialises external continuations without holding task.lock while a worker
    # thread needs that lock to report progress/completion.
    resume_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def public_dict(self) -> dict[str, Any]:
        with self.lock:
            return {
                "task_id": self.task_id,
                "plan_id": self.plan_id,
                "session_id": self.session_id,
                "parent_task_id": self.parent_task_id,
                "name": self.spec.agent_name,
                "status": self.status,
                "role": self.spec.role,
                "objective": self.spec.objective,
                "backend": self.spec.backend,
                "backend_id": self.spec.backend_id,
                "tool_policy": self.spec.tool_policy,
                "workspace_policy": self.spec.workspace_policy,
                "provider_id": self.provider_id,
                "model_id": self.model_id,
                "created_at": self.created_at,
                "started_at": self.started_at,
                "completed_at": self.completed_at,
                "generation": self.generation,
                "continuation_count": self.continuation_count,
                "depth": self.depth,
                "workspace_id": self.workspace_id,
                "workspace_path": self.workspace_path,
                "workspace_kind": self.workspace_kind,
                "workspace_state": self.workspace_state,
                "pending_messages": len(self.inbox),
                "cancel_requested": self.cancel_event.is_set(),
                "error": self.error,
                "result": self.result.public_dict() if self.result is not None else None,
            }


@dataclass(frozen=True)
class ExecutionPlan:
    """One turn's product-neutral execution graph."""

    decision: Any
    mode: str
    post_stage: Any | None
    rationale: tuple[str, ...]
    task_profile: TaskProfile
    agents: tuple[AgentSpec, ...]
    topology: str = "single"
    max_agents: int = 1
    max_parallel: int = 1
    max_depth: int = 1

    @property
    def primary(self) -> AgentSpec:
        return next((row for row in self.agents if row.role == "lead"), self.agents[0])

    @property
    def workers(self) -> tuple[AgentSpec, ...]:
        return tuple(row for row in self.agents if row.role == "worker")

    @property
    def worker(self) -> AgentSpec | None:
        return self.workers[0] if self.workers else None

    @property
    def reviewer(self) -> AgentSpec | None:
        return next((row for row in self.agents if row.role in {"review", "verify"}), None)


class AgentRegistry:
    """Thread-safe per-session task registry with stable continuation identity."""

    def __init__(self, *, max_plans: int = 64) -> None:
        self._lock = threading.RLock()
        self._tasks: dict[str, AgentTask] = {}
        self._role_index: dict[tuple[str, str], list[str]] = {}
        self._name_index: dict[tuple[str, str], str] = {}
        self._plan_order: list[str] = []
        self._max_plans = max(4, int(max_plans))

    def _prune_locked(self) -> None:
        while len(self._plan_order) > self._max_plans:
            plan_id = self._plan_order.pop(0)
            doomed = [task_id for task_id, task in self._tasks.items() if task.plan_id == plan_id]
            for task_id in doomed:
                task = self._tasks.pop(task_id, None)
                if task is None:
                    continue
                ids = self._role_index.get((plan_id, task.spec.role), [])
                self._role_index[(plan_id, task.spec.role)] = [row for row in ids if row != task_id]
                if not self._role_index[(plan_id, task.spec.role)]:
                    self._role_index.pop((plan_id, task.spec.role), None)
                self._name_index.pop((plan_id, task.spec.agent_name), None)

    def begin_plan(
        self, session_id: str, specs: Iterable[AgentSpec], *, parent_task_id: str = ""
    ) -> tuple[str, tuple[AgentTask, ...]]:
        plan_id = uuid.uuid4().hex
        tasks: list[AgentTask] = []
        seen_names: set[str] = set()
        with self._lock:
            self._plan_order.append(plan_id)
            for index, spec in enumerate(specs, 1):
                name = spec.agent_name
                if name in seen_names:
                    name = f"{name}-{index}"
                    spec = AgentSpec(**{**spec.__dict__, "name": name})
                seen_names.add(name)
                task = AgentTask(
                    task_id=uuid.uuid4().hex,
                    plan_id=plan_id,
                    session_id=session_id,
                    spec=spec,
                    parent_task_id=parent_task_id,
                )
                self._tasks[task.task_id] = task
                self._role_index.setdefault((plan_id, spec.role), []).append(task.task_id)
                self._name_index[(plan_id, spec.agent_name)] = task.task_id
                tasks.append(task)
            self._prune_locked()
        return plan_id, tuple(tasks)

    def _depth_locked(self, parent_task_id: str) -> int:
        depth = 0
        current = str(parent_task_id or "")
        seen: set[str] = set()
        while current:
            if current in seen:
                raise ValueError("agent parent cycle detected")
            seen.add(current)
            parent = self._tasks.get(current)
            if parent is None:
                break
            depth += 1
            current = parent.parent_task_id
        return depth

    def spawn_task(
        self, *, plan_id: str, session_id: str, spec: AgentSpec, parent_task_id: str = "",
        max_agents: int = 4, max_depth: int = 1,
    ) -> AgentTask:
        """Add one bounded child to an existing plan.

        This is the single dynamic-spawn boundary used by Agent Team. It does
        not execute the child; execution remains owned by :class:`AgentRuntime`.
        """
        with self._lock:
            plan_tasks = [row for row in self._tasks.values() if row.plan_id == plan_id]
            if len(plan_tasks) >= max(1, int(max_agents)):
                raise ValueError("agent team has reached max_agents")
            if parent_task_id:
                parent = self._tasks.get(parent_task_id)
                if parent is None or parent.plan_id != plan_id:
                    raise ValueError("unknown parent agent task")
                depth = int(parent.depth) + 1
            else:
                depth = 0
            if depth > max(1, int(max_depth)):
                raise ValueError("agent spawn exceeds max_depth")
            name = spec.agent_name
            if (plan_id, name) in self._name_index:
                name = f"{name}-{len(plan_tasks)+1}"
                spec = AgentSpec(**{**spec.__dict__, "name": name})
            task = AgentTask(
                task_id=uuid.uuid4().hex, plan_id=plan_id, session_id=session_id, spec=spec,
                parent_task_id=parent_task_id, depth=depth,
            )
            self._tasks[task.task_id] = task
            self._role_index.setdefault((plan_id, spec.role), []).append(task.task_id)
            self._name_index[(plan_id, spec.agent_name)] = task.task_id
            return task

    def set_parent(self, task_id: str, parent_task_id: str) -> AgentTask | None:
        with self._lock:
            task = self._tasks.get(str(task_id or ""))
            parent = self._tasks.get(str(parent_task_id or ""))
            if task is None or parent is None or task.plan_id != parent.plan_id or task.task_id == parent.task_id:
                return None
            depth = int(parent.depth) + 1
            current = parent
            seen = {task.task_id}
            while current is not None:
                if current.task_id in seen:
                    raise ValueError("agent parent cycle detected")
                seen.add(current.task_id)
                current = self._tasks.get(current.parent_task_id) if current.parent_task_id else None
            with task.lock:
                task.parent_task_id = parent.task_id
                task.depth = depth
            return task

    def set_workspace(
        self, task_id: str, *, path: str, kind: str, state: str = "ready", workspace_id: str = ""
    ) -> AgentTask | None:
        task = self.get(task_id)
        if task is None:
            return None
        with task.lock:
            task.workspace_id = str(workspace_id or task.workspace_id)
            task.workspace_path = str(path or "")
            task.workspace_kind = str(kind or "")
            task.workspace_state = str(state or "")
            return task

    def children(self, task_id: str) -> tuple[AgentTask, ...]:
        target = str(task_id or "")
        with self._lock:
            return tuple(
                row for row in self._tasks.values() if row.parent_task_id == target
            )

    def remove_task(self, task_id: str) -> bool:
        with self._lock:
            task = self._tasks.get(str(task_id or ""))
            if task is None or task.status == "running":
                return False
            if any(row.parent_task_id == task.task_id for row in self._tasks.values()):
                # Recycling a parent would leave descendants with a dangling
                # lineage and make depth/cycle checks unreliable. Recycle leaves
                # first, then their parent.
                return False
            self._tasks.pop(task.task_id, None)
            ids = self._role_index.get((task.plan_id, task.spec.role), [])
            self._role_index[(task.plan_id, task.spec.role)] = [row for row in ids if row != task.task_id]
            if not self._role_index[(task.plan_id, task.spec.role)]:
                self._role_index.pop((task.plan_id, task.spec.role), None)
            self._name_index.pop((task.plan_id, task.spec.agent_name), None)
            return True

    def get(self, task_id: str) -> AgentTask | None:
        with self._lock:
            return self._tasks.get(str(task_id or ""))

    def task_for_role(self, plan_id: str, role: str) -> AgentTask | None:
        tasks = self.tasks_for_role(plan_id, role)
        return tasks[0] if tasks else None

    def tasks_for_role(self, plan_id: str, role: str) -> tuple[AgentTask, ...]:
        with self._lock:
            ids = tuple(self._role_index.get((plan_id, role), ()))
            return tuple(self._tasks[row] for row in ids if row in self._tasks)

    def task_for_name(self, plan_id: str, name: str) -> AgentTask | None:
        with self._lock:
            task_id = self._name_index.get((plan_id, name))
            return self._tasks.get(task_id) if task_id else None

    def attach_context(self, task_id: str, messages: Sequence[dict[str, Any]]) -> bool:
        task = self.get(task_id)
        if task is None:
            return False
        with task.lock:
            # Shallow-copy message dictionaries; adapters treat nested content as
            # immutable request data and the child never mutates parent objects.
            task.context_messages = [dict(row) for row in messages if isinstance(row, dict)]
        return True

    def context_snapshot(self, task_id: str) -> tuple[dict[str, Any], ...]:
        task = self.get(task_id)
        if task is None:
            return ()
        with task.lock:
            return tuple(dict(row) for row in task.context_messages)

    def update_context(self, task_id: str, messages: Sequence[dict[str, Any]]) -> bool:
        return self.attach_context(task_id, messages)

    def pin_model(self, task_id: str, provider_id: str, model_id: str) -> AgentTask | None:
        task = self.get(task_id)
        if task is None:
            return None
        with task.lock:
            if task.spec.sticky_model and task.provider_id and task.model_id:
                return task
            task.provider_id = str(provider_id or task.provider_id)
            task.model_id = str(model_id or task.model_id)
            return task

    def transition(
        self,
        task_id: str,
        status: str,
        *,
        provider_id: str = "",
        model_id: str = "",
        result: AgentResult | None = None,
        error: str = "",
    ) -> AgentTask | None:
        if status not in _AGENT_STATES:
            raise ValueError(f"invalid agent task status: {status}")
        now = time.time()
        task = self.get(task_id)
        if task is None:
            return None
        with task.lock:
            if status == "running" and not task.started_at:
                task.started_at = now
            if status in _TERMINAL_STATES:
                task.completed_at = now
            elif status in {"planned", "paused"}:
                task.completed_at = 0.0
            task.status = status
            if provider_id:
                task.provider_id = provider_id
            if model_id:
                task.model_id = model_id
            if result is not None:
                task.result = result
            if error:
                task.error = error
            elif status in {"planned", "running", "completed"}:
                task.error = ""
            return task

    def send_message(self, task_id: str, message: str) -> AgentTask | None:
        text = str(message or "").strip()
        if not text:
            return self.get(task_id)
        task = self.get(task_id)
        if task is None:
            return None
        with task.lock:
            task.inbox.append(text)
            task.continuation_count += 1
        return task

    def consume_messages(self, task_id: str) -> tuple[str, ...]:
        task = self.get(task_id)
        if task is None:
            return ()
        with task.lock:
            values = tuple(task.inbox)
            task.inbox.clear()
            return values

    def cancel(self, task_id: str) -> AgentTask | None:
        task = self.get(task_id)
        if task is None:
            return None
        task.cancel_event.set()
        with task.lock:
            if task.status in {"planned", "paused"}:
                task.status = "cancelled"
                task.completed_at = time.time()
        return task

    def resume(self, task_id: str, *, message: str = "") -> AgentTask | None:
        task = self.get(task_id)
        if task is None:
            return None
        with task.lock:
            if task.status == "running":
                if message.strip():
                    task.inbox.append(message.strip())
                    task.continuation_count += 1
                return task
            task.cancel_event.clear()
            task.generation += 1
            task.status = "planned"
            task.started_at = 0.0
            task.completed_at = 0.0
            task.result = None
            task.error = ""
            if message.strip():
                task.inbox.append(message.strip())
                task.continuation_count += 1
            return task

    def list(self, *, plan_id: str = "") -> tuple[AgentTask, ...]:
        with self._lock:
            values = tuple(self._tasks.values())
        if plan_id:
            values = tuple(row for row in values if row.plan_id == plan_id)
        return tuple(sorted(values, key=lambda row: (row.created_at, row.task_id)))


class AgentTaskBusyError(RuntimeError):
    """A second execution must not be started for an in-flight child task."""


class AgentRuntime:
    """Bounded executor for child tasks.

    The runtime does not know providers, tools or policy packs. Callers supply a
    runner that consumes an :class:`AgentTask` and returns an :class:`AgentResult`.
    This keeps concurrency mechanics out of the Turn Engine and makes the same
    task contract reusable by future durable/background execution.
    """

    def __init__(self, registry: AgentRegistry, *, max_workers: int = 8) -> None:
        self.registry = registry
        self.max_workers = max(1, min(8, int(max_workers)))
        self._executor = ThreadPoolExecutor(max_workers=self.max_workers, thread_name_prefix="kiro-byo-agent")
        self._lock = threading.RLock()
        self._futures: dict[str, Future[AgentResult]] = {}

    def submit(
        self, task: AgentTask, runner: Callable[[AgentTask], AgentResult]
    ) -> Future[AgentResult]:
        def invoke() -> AgentResult:
            if task.cancel_event.is_set():
                result = AgentResult(status="cancelled")
                self.registry.transition(task.task_id, "cancelled", result=result)
                return result
            self.registry.transition(task.task_id, "running")
            try:
                result = runner(task)
                status = result.status if result.status in _AGENT_STATES else "completed"
                if task.cancel_event.is_set() and status == "completed":
                    status = "cancelled"
                    result = AgentResult(
                        status="cancelled", summary=result.summary,
                        findings=result.findings, evidence=result.evidence,
                        unresolved=result.unresolved, provider_id=result.provider_id,
                        model_id=result.model_id, duration_ms=result.duration_ms,
                        usage=result.usage,
                    )
                self.registry.transition(
                    task.task_id, status,
                    provider_id=result.provider_id, model_id=result.model_id, result=result,
                )
                return result
            except BaseException as exc:  # lifecycle must close even on adapter bugs
                result = AgentResult(
                    status="failed", provider_id=task.provider_id, model_id=task.model_id
                )
                self.registry.transition(
                    task.task_id, "failed", result=result,
                    error=f"{type(exc).__name__}: {exc}",
                )
                raise

        # The lookup, admission and new Future registration are one critical
        # section. Otherwise two concurrent resume calls may both submit the
        # same child before either has transitioned its status to "running".
        with self._lock:
            previous = self._futures.get(task.task_id)
            if previous is not None and not previous.done():
                raise AgentTaskBusyError(f"agent task is already executing: {task.task_id}")
            future = self._executor.submit(invoke)
            self._futures[task.task_id] = future
        return future

    def is_active(self, task_id: str) -> bool:
        with self._lock:
            future = self._futures.get(str(task_id or ""))
            return future is not None and not future.done()

    def run_group(
        self,
        tasks: Sequence[AgentTask],
        runner: Callable[[AgentTask], AgentResult],
        *,
        max_parallel: int | None = None,
    ) -> tuple[tuple[AgentTask, AgentResult], ...]:
        if not tasks:
            return ()
        # A temporary gate lets a session policy request less parallelism than
        # the executor's process-wide hard cap without creating nested pools.
        gate = threading.Semaphore(max(1, min(self.max_workers, int(max_parallel or self.max_workers))))

        def gated(task: AgentTask) -> AgentResult:
            with gate:
                return runner(task)

        futures = {self.submit(task, gated): task for task in tasks}
        results: dict[str, AgentResult] = {}
        for future in as_completed(futures):
            task = futures[future]
            try:
                results[task.task_id] = future.result()
            except BaseException:
                current = self.registry.get(task.task_id)
                if future.cancelled() or (current is not None and current.status == "cancelled"):
                    # Cancellation while waiting for a worker slot is not a
                    # provider failure; do not falsely report the task failed.
                    results[task.task_id] = AgentResult(
                        status="cancelled", provider_id=task.provider_id, model_id=task.model_id,
                    )
                else:
                    results[task.task_id] = (
                        current.result if current is not None and current.result is not None
                        else AgentResult(status="failed", provider_id=task.provider_id, model_id=task.model_id)
                    )
        return tuple((task, results[task.task_id]) for task in tasks)

    def cancel(self, task_id: str) -> bool:
        task = self.registry.cancel(task_id)
        with self._lock:
            future = self._futures.get(task_id)
        if future is not None:
            future.cancel()
        return task is not None

    def shutdown(self, *, wait: bool = False) -> None:
        self._executor.shutdown(wait=wait, cancel_futures=True)
