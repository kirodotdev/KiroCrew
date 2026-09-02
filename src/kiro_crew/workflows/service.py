"""Gateway-side workflow service: the shared registry + runner the chat tools,
the Workflows app, and result-to-chat injection all talk to.

This is the single place the gateway wires the dynamic-workflows engine into the
live process: one ``RunRegistry``, a ``WorkflowRunner`` whose ``agent_fn`` runs
``ctx.agent()`` steps through the real ``SessionManager``, and a couple of
narrow async entry points (``author``, ``start``, ``status``, ``result``,
``cancel``, ``list_runs``).

Kept as a small façade so the dashboard handlers stay thin and the MCP tools
have a stable target. The gateway constructs ONE ``WorkflowService`` at startup
(``DashboardState.workflow_service``) and the handlers reach it via the request
app state — exactly like ``state.subagents`` / ``state.sessions``.

``author`` turns a natural-language intent into a validated workflow script using
the same in-session model the rest of KiroCrew uses (no new model plumbing). It
loops up to a few times until the produced script passes ``validate`` — the
authoring-reliability gates (G1/G2/G3) cover this shape.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import threading
from typing import Any, Callable, Optional

from kiro_crew import autonudge
from kiro_crew.acp.client import AcpError
from kiro_crew.acp.runtime import AcpRequestTimeout, AcpRuntimeDead
from kiro_crew.llm_helpers import (
    ToolApprovalPolicy,
    acp_error_is_transient,
    stream_and_collect,
)
from kiro_crew.task_planner import decompose_yaml

from .agent_exec import build_agent_fn
from .agent_pool import build_pooled_agent_fn
from .events import EventStream
from .library import (
    SOURCE_FORMAT_PYTHON,
    SOURCE_FORMAT_TASK_PLAN,
    SensitiveWorkflowSourceError,
    WorkflowDefinitionLibrary,
)
from .registry import (
    STATUS_CANCELLED,
    STATUS_FAILED,
    STATUS_FINISHED,
    STATUS_PAUSED,
    STATUS_RUNNING,
    RunHandle,
    RunRegistry,
)
from .runner import WorkflowRunner, clamp_run_timeout
from .store import WorkflowRunStore
from .validate import validate

logger = logging.getLogger(__name__)

# Bounded attempts to coax a valid script out of the model (mirrors schema retry).
_AUTHOR_RETRIES = 2
_AUTHOR_REFERENCE_LIMIT = 3
_AUTHOR_REFERENCE_SOURCE_CHARS = 16000
# Startup retries are narrower than validation retries: only ACP control-plane
# transients qualify, and every retry receives a fresh isolated session key.
_AUTHOR_STARTUP_ATTEMPTS = 3
_AUTHOR_STARTUP_BACKOFF_SECS = (0.25, 0.75)


def _author_startup_retryable(exc: BaseException) -> bool:
    if isinstance(exc, (AcpRequestTimeout, AcpRuntimeDead)):
        return True
    return isinstance(exc, AcpError) and acp_error_is_transient(exc)


_AUTHOR_SYSTEM = """\
You are authoring a KiroCrew DYNAMIC WORKFLOW: one Python module that orchestrates
agents. Reply with ONLY the Python module (no prose, no code fence). It MUST be:

  META = {{"name": "<kebab-name>", "description": "<one line>", "phases": [...]}}
  async def workflow(ctx):
      ...
      return <json-serializable result>

Rules (the sandbox REJECTS violations): no imports; no open/eval/exec/__import__;
no dunder access; no .format/.format_map (use an f-string); no time/random/uuid
(use ctx.now / ctx.args). Use ONLY the ctx
surface: await ctx.agent(prompt, schema=?, label=?, phase=?), await ctx.parallel([..]),
await ctx.pipeline(items, *stages), ctx.phase(t), ctx.log(m),
ctx.nudge(idle_secs=?, message=?), ctx.budget, ctx.args. Nothing else exists on
ctx in this runtime — any other ctx attribute or method fails at runtime, so do
not invent one.

ASYNC vs SYNC — get this right or the script crashes at runtime:
  * AWAIT these (they are async): ctx.agent(...), ctx.parallel(...),
    ctx.pipeline(...).
  * Do NOT await these (they are SYNCHRONOUS and return a context manager):
    ctx.phase(title), ctx.log(msg), ctx.nudge(...). NEVER
    ``await ctx.phase("read")`` (awaiting raises TypeError immediately).
    Both calling styles work:
      ctx.phase("read")           # bare call — sets the phase
      with ctx.phase("read"):    # context manager — purely cosmetic grouping
          ...                     # (no-op: phase is NOT auto-ended on block exit)

RESULTS CAN BE None — always guard before use: ctx.agent(...) returns None when
the agent dies or its output fails schema validation (after retries); a failed
thunk inside ctx.parallel(...) resolves to None. So NEVER subscript, ``.get()``,
or attribute-access an awaited result inline. Bind it first and guard:
  BAD:  url = (await ctx.agent("cut cr")).get("cr_url")        # crashes if None
  BAD:  src = (await ctx.parallel([...]))[0]["client_py"]      # crashes if None
  GOOD: cr = await ctx.agent("cut cr")
        url = cr.get("cr_url", "UNKNOWN") if isinstance(cr, dict) else "UNKNOWN"
  GOOD: reads = await ctx.parallel([...])
        first = reads[0] if reads and reads[0] else {{}}
        src = first.get("client_py", "") if isinstance(first, dict) else ""

Prefer ctx.pipeline for multi-stage work; ctx.parallel is a
barrier. ctx.parallel accepts a list of ctx.agent(...) calls directly, e.g.
``await ctx.parallel([ctx.agent(p1), ctx.agent(p2)])`` (thunks like
``lambda: ctx.agent(p)`` also work). Give each agent a UNIQUE, specific label —
when fanning out, include the per-item identity (e.g. an index or the claim/item
text), NOT just a shared category, so the live progress tree shows distinct rows:
``label="verify:c" + str(i) + ":" + claim[:30]`` rather than ``label="verify:" + facet``.

Keep agent count LEAN — agents are expensive and the host is resource-constrained.
Do NOT spawn a separate agent per claim/source. For verification use a GENERATOR +
CRITIC pair: ONE agent produces the draft/findings for a topic, then ONE critic
agent reviews/fact-checks that whole output and returns corrections — not one
verifier per individual claim. So a research workflow is roughly: one generator
per top-level facet (a handful), then one critic per facet to challenge it, then a
single synthesis agent. Prefer few strong agents over many tiny ones.

Available builtins (NOTHING else — any other name is a NameError at runtime, so do
NOT reference json, math, os, datetime, re, etc.): len, range, enumerate, zip, map,
filter, sorted, reversed, sum, min, max, abs, round, all, any, bool, int, float,
str, list, tuple, dict, set, frozenset, isinstance, issubclass, repr. Exceptions you
MAY use in try/except/raise: Exception, ValueError, TypeError, KeyError, IndexError,
RuntimeError, and the other standard error types. You may define plain module-level
helper functions. There is NO json module — return plain dicts/lists directly.

Before you reply, RE-READ your script and fix these specific failure modes: (1) any
``await ctx.phase/log/nudge`` (those are sync — drop the await); (2) any subscript,
``.get()``, or attribute access on an awaited
ctx.agent/parallel/pipeline result that is not first bound to a
variable and None-guarded; (3) any use of a name that
is not a listed builtin, ctx, or a helper you defined. Reply ONLY when the script
is clean on all three.

Author the workflow for this task:

{intent}

{references}
"""


class WorkflowService:
    """Owns the shared run registry + runner for the gateway process."""

    def __init__(
        self,
        *,
        sessions: Any,
        on_done: Optional[Callable[[str, dict], None]] = None,
        on_event: Optional[Callable[[str, dict], None]] = None,
        now_fn: Optional[Callable[[], str]] = None,
        concurrency: Optional[int] = None,
        persist: bool = True,
        store: Any = None,
        pool_agents: bool = True,
        nudge_authorizer: Optional[Callable[..., Any]] = None,
        timeout_secs: Optional[int] = None,
        definition_library: Any = None,
        task_runner: Any = None,
    ) -> None:
        # Durable store: runs are mirrored to disk so they survive gateway
        # restarts. Pass persist=False (or store=None) to keep a purely in-memory
        # registry — used by tests that don't want filesystem side effects.
        if store is None and persist:
            try:
                store = WorkflowRunStore()
            except Exception:  # noqa: BLE001 - persistence is best-effort
                store = None
        self.registry = RunRegistry(store=store)
        self._definition_library = (
            definition_library if definition_library is not None else WorkflowDefinitionLibrary()
        )
        # Library calls run in worker threads from async gateway paths. Preserve
        # the single-writer ordering the event loop previously provided so slug
        # allocation and optimistic revision checks remain atomic in-process.
        self._definition_lock = threading.RLock()
        if on_done is not None:
            self.registry.set_on_done(on_done)
        if on_event is not None:
            self.registry.set_on_event(on_event)
        self._sessions = sessions
        self._task_runner = task_runner
        # Injected by the gateway: an async ``(*, slot_key, message, idle_secs,
        # max_cycles) -> None`` that runs the SHARED authorize_and_add_nudge
        # chokepoint (ownership/allowlist checks + message limit + SEL audit)
        # before arming an AutoNudge loop. None in tests / non-dashboard hosts →
        # ``ctx.nudge`` degrades to a logged no-op. Strong refs to in-flight arm
        # tasks are held here BUCKETED PER RUN (run_id → tasks) so each run's
        # teardown drains its own arms before the terminal transition.
        self._nudge_authorizer = nudge_authorizer
        self._nudge_tasks: dict[str, set[Any]] = {}
        self._now_fn = now_fn or (lambda: "1970-01-01T00:00:00Z")
        self._concurrency = concurrency
        # Default wall-clock ceiling for runs started through this service. The
        # ceiling is a runaway backstop, so a deployment (or an individual run) can
        # LENGTHEN it for genuinely long investigations but never remove it —
        # clamp_run_timeout keeps every value inside [MIN, MAX].
        self._timeout_secs = clamp_run_timeout(timeout_secs)
        # When True, each run's ``ctx.agent()`` calls reuse a small WARM session
        # pool instead of cold-starting a fresh session per call (kills the
        # per-call cold-start that dominates workflow wall-clock — see
        # workflows/agent_pool.py). The pool is per-run and torn down when the run
        # ends. Off => the original cold-start-per-call path (build_agent_fn).
        self._pool_agents = pool_agents
        self._seq = 0
        self._host_streams: dict[str, EventStream] = {}
        # Rehydrate any persisted runs from a prior process, and continue the
        # run-id sequence past the highest seen so new ids never collide.
        try:
            n = self.registry.load_persisted()
            if n:
                self._seq = self._max_persisted_seq()
        except Exception:  # noqa: BLE001
            pass

    @property
    def timeout_secs(self) -> int:
        """Effective (clamped) default wall-clock ceiling for runs of this service."""
        return self._timeout_secs

    def _max_persisted_seq(self) -> int:
        """Highest wf_NNNNNN sequence among loaded runs (so new ids don't collide)."""
        hi = 0
        for snap in self.registry.list():
            rid = snap.get("run_id", "")
            if rid.startswith("wf_"):
                tail = rid[3:]
                if tail.isdigit():
                    hi = max(hi, int(tail))
        return hi

    def _new_run_id(self) -> str:
        # Deterministic, monotonic per process (no time/random — resume-stable).
        self._seq += 1
        return f"wf_{self._seq:06d}"

    async def begin_host_run(
        self,
        *,
        name: str,
        source: str = "",
        source_format: str,
        task_id: str = "",
        driver: str,
        author: str = "",
        session_key: str = "",
        capabilities: tuple[str, ...] = (),
        workflow_id: str = "",
        workflow_slug: str = "",
        workflow_revision: int = 0,
        derived_from_workflow_id: str = "",
        derived_from_revision: int = 0,
    ) -> str:
        """Register work executed by a trusted host on the shared run substrate.

        Host runs publish lifecycle and progress only. Their driver keeps all
        product semantics, including planning, approvals, retries, and cleanup.
        """
        run_id = self._new_run_id()
        handle = RunHandle(
            run_id=run_id,
            name=name or run_id,
            author=author,
            session_key=session_key,
            source=source,
            source_format=source_format,
            driver=driver,
            task_id=task_id,
            capabilities=capabilities,
            completion_injection=False,
            workflow_id=workflow_id,
            workflow_slug=workflow_slug,
            workflow_revision=workflow_revision,
            derived_from_workflow_id=derived_from_workflow_id,
            derived_from_revision=derived_from_revision,
        )
        self.registry.register(handle, persist=False)
        stream = EventStream(run_id)
        self._host_streams[run_id] = stream
        script_hash = hashlib.sha256(source.encode("utf-8")).hexdigest() if source else ""
        self.registry.record_event(
            run_id,
            stream.run_started(
                self._now_fn(),
                name=handle.name,
                args={},
                script_hash=script_hash,
                budget_total=None,
            ),
            persist=False,
        )
        persist_task = asyncio.create_task(self.registry.persist_async(run_id))
        try:
            await asyncio.shield(persist_task)
        except BaseException:
            # The worker write cannot be cancelled once dispatched. Drain it
            # before deleting so a late atomic replace cannot resurrect a host
            # run whose registration never returned to its driver.
            try:
                await asyncio.shield(persist_task)
            finally:
                self._host_streams.pop(run_id, None)
                await asyncio.shield(self.registry.delete_async(run_id))
            raise
        return run_id

    async def bind_task(self, run_id: str, task: "asyncio.Task[Any]", *, task_id: str = "") -> bool:
        """Bind cancellation of a shared run to its host driver's live task."""
        handle = self.registry.get(run_id)
        if handle is None:
            return False
        handle.task = task
        if task_id:
            handle.task_id = task_id
        await self.registry.persist_async(run_id)
        return True

    def _host_stream(self, run_id: str) -> Optional[EventStream]:
        """Return a stream that continues a host run's persisted sequence."""
        stream = self._host_streams.get(run_id)
        if stream is not None:
            return stream
        handle = self.registry.get(run_id)
        if handle is None or handle.driver == "workflow":
            return None
        next_seq = max((event.seq for event in handle.events), default=-1) + 1
        stream = EventStream(run_id, starting_seq=next_seq)
        self._host_streams[run_id] = stream
        return stream

    async def phase(self, run_id: str, title: str) -> None:
        stream = self._host_stream(run_id)
        if stream is not None:
            await self.registry.record_event_async(
                run_id, stream.phase_started(self._now_fn(), title=title)
            )

    async def log(self, run_id: str, message: str) -> None:
        stream = self._host_stream(run_id)
        if stream is not None:
            await self.registry.record_event_async(
                run_id, stream.log(self._now_fn(), message=message)
            )

    async def set_source(
        self,
        run_id: str,
        source: str,
        *,
        source_format: str = "",
        clear_definition: bool = False,
    ) -> bool:
        handle = self.registry.get(run_id)
        if handle is None:
            return False
        handle.source = source
        if source_format:
            handle.source_format = source_format
        if clear_definition:
            if (
                handle.workflow_id
                and handle.workflow_revision
                and not handle.derived_from_workflow_id
            ):
                handle.derived_from_workflow_id = handle.workflow_id
                handle.derived_from_revision = handle.workflow_revision
            handle.workflow_id = ""
            handle.workflow_slug = ""
            handle.workflow_revision = 0
        # TaskRunner plans can reach the 256 KiB request ceiling. Persisting that
        # source performs JSON encoding plus an atomic disk write, so keep the
        # loop-affine mutation above on the event loop and move only the durable
        # mirror to a worker thread.
        await self.registry.persist_async(run_id)
        return True

    async def step(
        self,
        run_id: str,
        index: int,
        title: str,
        *,
        status: str,
        result: str = "",
        error: str = "",
    ) -> None:
        """Publish one host step using the common agent progress vocabulary."""
        stream = self._host_stream(run_id)
        if stream is None:
            return
        agent_id = f"taskrunner:{index}"
        if status == STATUS_RUNNING:
            snapshot = self.registry.status(run_id) or {}
            event = stream.agent_started(
                self._now_fn(),
                agent_id=agent_id,
                label=title,
                phase=snapshot.get("phase", ""),
                call_index=index,
            )
        else:
            event = stream.agent_finished(
                self._now_fn(),
                agent_id=agent_id,
                result_summary=result[:120],
                ok=status == STATUS_FINISHED,
                error=error,
            )
        await self.registry.record_event_async(run_id, event)

    async def pause(self, run_id: str) -> bool:
        handle = self.registry.get(run_id)
        if handle is not None:
            handle.task = None
        if not self.registry.set_status(run_id, STATUS_PAUSED, persist=False):
            return False
        await self.registry.persist_async(run_id)
        return True

    async def rebind(self, run_id: str, task: "asyncio.Task[Any]", *, task_id: str = "") -> bool:
        if not self.registry.set_status(run_id, STATUS_RUNNING, persist=False):
            if not self.registry.reopen_host_run(run_id, task_id=task_id, persist=False):
                return False
        self._host_stream(run_id)
        return await self.bind_task(run_id, task, task_id=task_id)

    async def finish(self, run_id: str, result: Any) -> None:
        stream = self._host_streams.pop(run_id, None)
        if stream is not None:
            await self.registry.record_event_async(
                run_id,
                stream.run_finished(self._now_fn(), result=result, duration_s=0.0),
            )
        await self.registry.mark_terminal_async(run_id, STATUS_FINISHED, result=result)

    async def fail(self, run_id: str, error: str, *, where: str = "host") -> None:
        stream = self._host_streams.pop(run_id, None)
        if stream is not None:
            await self.registry.record_event_async(
                run_id,
                stream.run_failed(self._now_fn(), error=error, where=where),
            )
        await self.registry.mark_terminal_async(run_id, STATUS_FAILED, error=error)

    async def cancel_host_run(self, run_id: str, reason: str = "cancelled") -> None:
        stream = self._host_streams.pop(run_id, None)
        if stream is not None:
            await self.registry.record_event_async(
                run_id,
                stream.run_cancelled(self._now_fn(), reason=reason),
            )
        await self.registry.mark_terminal_async(run_id, STATUS_CANCELLED, error=reason)

    async def delete_run(self, run_id: str) -> bool:
        """Delete a shared run after its owning product has stopped it."""
        self._host_streams.pop(run_id, None)
        self._nudge_tasks.pop(run_id, None)
        return await self.registry.delete_async(run_id)

    def _nudge_port(
        self,
        *,
        run_id: str,
        session_key: str,
        idle_secs: int,
        message: str,
        max_cycles: int = 0,
        notify: Optional[Callable[[str], None]] = None,
    ) -> None:
        """Wire ``ctx.nudge`` → AutoNudge: arm a same-session monitoring loop on
        the workflow's ORIGINATING session (dashboard slot / channel).

        SECURITY: ``session_key`` is caller-influenced (workflow endpoints derive
        it from the ``X-Session-Key`` header), so this MUST NOT arm a loop
        directly — it delegates to the gateway-injected ``nudge_authorizer``,
        which runs the same authorize_and_add_nudge chokepoint the REST handler
        uses (dashboard-slot existence, Slack routability, Discord allowlist +
        current-session match, 8000-char limit, SEL audit). Without that, a
        workflow could spoof another session's key and mint a loop on it.

        VISIBILITY: every outcome — armed, skipped, denied, failed — is reported
        through ``notify`` (the runner passes a ctx-log emitter), so the run's
        event stream shows what actually happened instead of a silent no-op the
        user mistakes for an armed monitor. Best-effort semantics otherwise
        unchanged: a monitoring convenience never crashes a completed
        orchestration.

        Called synchronously from inside the running workflow coroutine, so the
        async authorizer is scheduled as a task on the live loop (a strong ref is
        kept so it isn't GC'd).
        """

        def _say(msg: str) -> None:
            if notify is None:
                return
            try:
                notify(msg)
            except Exception:  # noqa: BLE001 - visibility must never break the run
                logger.debug("ctx.nudge notify failed", exc_info=True)

        authorizer = self._nudge_authorizer
        if authorizer is None:
            logger.warning("workflow ctx.nudge skipped: no nudge authorizer wired")
            _say("ctx.nudge NOT armed: no nudge authorizer wired in this runtime")
            return
        binding = autonudge.binding_key_for(session_key)
        if not binding:
            logger.warning("workflow ctx.nudge skipped: session %r is not nudge-able", session_key)
            _say(
                f"ctx.nudge NOT armed: originating session {session_key!r} is not "
                "nudge-able (only dashboard/Slack/Discord sessions can be nudged)"
            )
            return

        async def _arm() -> None:
            try:
                error = await authorizer(
                    slot_key=binding,
                    message=message,
                    idle_secs=idle_secs,
                    max_cycles=max_cycles,
                )
            except asyncio.CancelledError:
                # Drain timeout at run teardown cancelled this wrapper while the
                # underlying (shielded, service-supervised) add may still land.
                # Record the indeterminacy in the run BEFORE the terminal event
                # so the stream never implies a resolved outcome it doesn't have.
                _say(
                    "ctx.nudge outcome undetermined: the run ended before arming "
                    "completed — the loop may still arm (check the AutoNudge panel)"
                )
                raise
            except Exception:  # noqa: BLE001 - arming must never break the run
                logger.warning("workflow ctx.nudge: failed to arm loop", exc_info=True)
                _say("ctx.nudge NOT armed: internal error while arming the loop")
                return
            if error:
                _say(f"ctx.nudge NOT armed: {error}")
            else:
                _say(
                    f"ctx.nudge armed: monitoring loop on this session "
                    f"(idle {int(idle_secs)}s"
                    + (f", max {int(max_cycles)} cycles" if max_cycles else "")
                    + ")"
                )

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - workflows always run in a loop
            logger.warning("workflow ctx.nudge skipped: no running event loop")
            return
        task = loop.create_task(_arm())
        # Retain a strong ref until completion (asyncio may GC a bare task),
        # bucketed PER RUN so the run's teardown can drain its own arms before
        # the terminal transition (outcome logs land in the run record, and no
        # arm outlives the run / gateway shutdown unsupervised).
        bucket = self._nudge_tasks.setdefault(run_id, set())
        bucket.add(task)
        task.add_done_callback(bucket.discard)

    async def _drain_nudge_tasks(self, run_id: str, timeout: float = 10.0) -> None:
        """Await the run's in-flight ctx.nudge arms before its terminal transition.

        Bounded: a stuck arm is cancelled after ``timeout`` so run teardown can
        never wedge. Draining (rather than cancelling outright) is deliberate —
        the arm is authorize+add with shield-protected persistence, so awaiting
        yields a definite, SEL-audited, run-logged outcome, while cancelling
        mid-authorize would reintroduce partial-arm ambiguity. A cancelled run
        whose arm already landed keeps the documented "mutation may have already
        landed" semantics.
        """
        tasks = self._nudge_tasks.pop(run_id, set())
        pending = [t for t in tasks if not t.done()]
        if not pending:
            return
        _done, still_pending = await asyncio.wait(pending, timeout=timeout)
        for t in still_pending:  # pragma: no cover - defensive bound
            t.cancel()
            logger.warning("workflow %s: cancelled stuck ctx.nudge arm at teardown", run_id)
        if still_pending:
            # Let the cancelled wrappers run their CancelledError handlers NOW so
            # their "outcome undetermined" events land BEFORE the terminal event.
            # The underlying shielded add stays supervised by AutoNudgeService.
            await asyncio.gather(*still_pending, return_exceptions=True)

    def _runner(self, run_id: str, *, timeout_secs: Optional[int] = None) -> WorkflowRunner:
        # ``timeout_secs`` overrides the service default for THIS run only (clamped
        # into [MIN, MAX] so a per-run value can lengthen the ceiling but never
        # remove it). None → the service default.
        ceiling = clamp_run_timeout(timeout_secs, default=self._timeout_secs)

        # Native ports wired for every run of this service. ``nudge`` bridges
        # ``ctx.nudge`` to AutoNudge (the port is session-agnostic — the runner
        # supplies each run's originating session_key at call time; the closure
        # binds run_id so arms are tracked per run and drained at teardown).
        def _nudge(**kw: Any) -> None:
            self._nudge_port(run_id=run_id, **kw)

        ports = {"nudge": _nudge}

        async def _drain() -> None:
            # Awaited by the runner BEFORE each terminal event: nudge outcome
            # logs land inside the stream contract (terminal events are last).
            await self._drain_nudge_tasks(run_id)

        agent_fn: Optional[Callable[[str, dict], Any]] = None
        pool: Any = None
        if self._pool_agents:
            try:
                # Size the warm pool to the run's fan-out cap so a fully-parallel
                # wave gets a distinct warm worker each, and sequential steps reuse.
                workers = self._concurrency if self._concurrency and self._concurrency > 0 else 4
                agent_fn, pool = build_pooled_agent_fn(
                    self._sessions,
                    run_id=run_id,
                    max_workers=workers,
                    max_starting=min(workers, 2),
                )
            except Exception:  # noqa: BLE001 - never let pooling break run start
                agent_fn, pool = None, None
                logger.warning(
                    "workflow agent pool init failed for %s; falling back to per-call sessions",
                    run_id,
                    exc_info=True,
                )
        # Pooling off, or its init raised: cold-start a session per call instead.
        # Keyed on ``agent_fn``, so a pool that yields no executor is not used.
        if agent_fn is None:
            agent_fn = build_agent_fn(self._sessions, run_id=run_id)

        async def _teardown() -> None:
            # Safety net (drain is a no-op if pre_terminal already ran; covers
            # pre-exec failure paths), then release the warm pool if there is one.
            await self._drain_nudge_tasks(run_id)
            if pool is not None:
                await pool.shutdown()

        return WorkflowRunner(
            agent_fn=agent_fn,
            concurrency=self._concurrency,
            timeout_secs=ceiling,
            ports=ports,
            pre_terminal=_drain,
            on_complete=_teardown,
        )

    async def author(
        self,
        intent: str,
        *,
        author: str = "",
        on_progress: Optional[Callable[[str], None]] = None,
    ) -> dict:
        """Turn a NL intent into a validated workflow script (or report errors).

        ``on_progress(msg)`` streams human-readable authoring progress (each
        attempt, retries) so author-in-run can surface it live in the sidebar/chat.
        """

        def _say(msg: str) -> None:
            if on_progress is not None:
                try:
                    on_progress(msg)
                except Exception:  # noqa: BLE001 - progress must never break authoring
                    pass

        # Authoring runs in a FRESH, ISOLATED, ephemeral session — never the shared
        # _bg session — so a workflow stays fully independent: its authoring context
        # never pollutes (or is polluted by) chat, consolidation, or other runs.
        #
        # Cost: authoring is pure text generation (intent → Python script), so it
        # uses the tool-less ``kirocrew-lite`` agent. That is the lever that makes a
        # fresh session cheap: the dominant cold-start cost was loading the full
        # MCP toolset + system prompt; lite carries no tools, so the turn is just
        # the generation. REJECT_ALL is belt-and-suspenders against an alternate
        # ACP backend injecting tools without set_mode. The session is destroyed
        # the instant authoring finishes — nothing remains registered or resumable.
        provider: Any = None
        key = ""
        for startup_attempt in range(1, _AUTHOR_STARTUP_ATTEMPTS + 1):
            # A fresh key per attempt prevents a half-created session from being
            # reclaimed after a timeout. SessionManager owns provider hard-kill
            # cleanup when startup fails before registration.
            key = f"wf-author:{self._new_run_id()}:a{startup_attempt}"
            try:
                provider, *_ = await self._sessions.get_or_create(key, agent="kirocrew-lite")
            except Exception as exc:
                try:
                    await self._sessions.destroy(key)
                except Exception:  # noqa: BLE001 - preserve the startup failure
                    logger.debug("workflow author startup destroy failed", exc_info=True)
                if (
                    not _author_startup_retryable(exc)
                    or startup_attempt >= _AUTHOR_STARTUP_ATTEMPTS
                ):
                    logger.warning(
                        "workflow_author_startup attempt=%d/%d outcome=failed "
                        "exception=%s retryable=%s",
                        startup_attempt,
                        _AUTHOR_STARTUP_ATTEMPTS,
                        type(exc).__name__,
                        _author_startup_retryable(exc),
                    )
                    raise
                delay = _AUTHOR_STARTUP_BACKOFF_SECS[startup_attempt - 1]
                logger.warning(
                    "workflow_author_startup attempt=%d/%d outcome=retry "
                    "exception=%s backoff_s=%.2f",
                    startup_attempt,
                    _AUTHOR_STARTUP_ATTEMPTS,
                    type(exc).__name__,
                    delay,
                )
                _say(
                    "Author session startup was temporarily unavailable — "
                    f"retrying ({startup_attempt + 1}/{_AUTHOR_STARTUP_ATTEMPTS})…"
                )
                await asyncio.sleep(delay)
                continue
            if startup_attempt > 1:
                logger.info(
                    "workflow_author_startup attempt=%d/%d outcome=recovered",
                    startup_attempt,
                    _AUTHOR_STARTUP_ATTEMPTS,
                )
            break

        try:
            errors: list[str] = []
            source = ""
            attempts = _AUTHOR_RETRIES + 1
            for i in range(attempts):
                if errors:
                    _say(
                        f"Script was invalid ({'; '.join(errors)}) — revising (attempt {i + 1}/{attempts})…"
                    )
                else:
                    _say(f"Drafting the workflow script (attempt {i + 1}/{attempts})…")
                matches = await asyncio.to_thread(self._search_definitions, intent)
                references = _authoring_references(matches)
                prompt = _AUTHOR_SYSTEM.format(intent=intent, references=references)
                if errors:
                    prompt += f"\n\nYour previous script was INVALID: {'; '.join(errors)}. Fix it."
                text = await stream_and_collect(
                    provider, prompt, approval_policy=ToolApprovalPolicy.REJECT_ALL
                )
                source = _strip_fence(text)
                vr = validate(source)
                if vr.ok:
                    _say(f"Script validated: {(vr.meta or {}).get('name', 'workflow')}")
                    # The model may claim only a reference it actually saw in
                    # this authoring attempt. Historical revisions remain valid
                    # for explicit/session promotion, but they are not included
                    # in the prompt and therefore cannot establish provenance.
                    derived_from = _verified_lineage(vr.meta or {}, matches)
                    return {
                        "ok": True,
                        "source": source,
                        "meta": vr.meta,
                        "derived_from": derived_from,
                    }
                errors = vr.errors
            return {"ok": False, "errors": errors, "source": source}
        finally:
            # Destroy, rather than merely release, the acquired lease: release()
            # leaves the provider and registry entry alive until idle expiry.
            try:
                await self._sessions.destroy(key)
            except asyncio.CancelledError:
                raise
            except Exception:
                # SessionManager.destroy remains the hard-cleanup owner. This
                # boundary is housekeeping only: an ordinary teardown failure
                # must not replace a successful script or the primary authoring
                # exception that brought execution here.
                logger.warning("workflow author teardown failed", exc_info=True)

    async def start_from_intent(
        self,
        intent: str,
        *,
        name: str = "",
        args: Optional[dict] = None,
        author: str = "",
        session_key: str = "",
        budget_total: Optional[int] = None,
        timeout_secs: Optional[int] = None,
    ) -> dict:
        """Launch a background run that AUTHORS its own script from ``intent``.

        Returns ``{run_id}`` immediately — authoring happens inside the run as a
        visible "Authoring" phase, so the slow model call(s) never block this call
        (no more 30s synchronous-author timeout) and progress streams to the UI.

        ``timeout_secs`` raises or lowers the wall-clock ceiling for this run only
        (clamped); a long multi-phase investigation can be given more room without
        changing the default for everything else.
        """
        if not intent.strip():
            return {"error": "intent is required"}
        run_id = self._new_run_id()

        async def _author_fn(
            it: str, *, on_progress: Optional[Callable[[str], None]] = None
        ) -> dict:
            return await self.author(it, author=author, on_progress=on_progress)

        await self._runner(run_id, timeout_secs=timeout_secs).run_background(
            "",  # no source — author inside the run
            registry=self.registry,
            run_id=run_id,
            now=self._now_fn(),
            name=name or run_id,
            args=args or {},
            author=author,
            session_key=session_key,
            budget_total=budget_total,
            intent=intent,
            author_fn=_author_fn,
        )
        return {"run_id": run_id, "name": name or ""}

    async def start(
        self,
        source: str,
        *,
        name: str = "",
        args: Optional[dict] = None,
        author: str = "",
        session_key: str = "",
        budget_total: Optional[int] = None,
        timeout_secs: Optional[int] = None,
        workflow_id: str = "",
        workflow_slug: str = "",
        workflow_revision: int = 0,
    ) -> dict:
        """Validate + launch a background run; return {run_id} or {error}.

        ``timeout_secs`` overrides the wall-clock ceiling for this run only (clamped
        into the runner's bounds).
        """
        vr = validate(source)
        if not vr.ok:
            return {"error": "; ".join(vr.errors), "errors": vr.errors}
        run_id = self._new_run_id()
        await self._runner(run_id, timeout_secs=timeout_secs).run_background(
            source,
            registry=self.registry,
            run_id=run_id,
            now=self._now_fn(),
            name=name or (vr.meta or {}).get("name", "") or run_id,
            args=args or {},
            author=author,
            session_key=session_key,
            budget_total=budget_total,
            workflow_id=workflow_id,
            workflow_slug=workflow_slug,
            workflow_revision=workflow_revision,
        )
        return {"run_id": run_id, "name": name or (vr.meta or {}).get("name", "")}

    def list_definitions(self, search: str = "") -> list[dict[str, Any]]:
        """List the global saved library, optionally ranked for a search intent."""
        with self._definition_lock:
            if search.strip():
                return self._definition_library.search(search)
            return self._definition_library.list()

    def _search_definitions(self, intent: str) -> list[dict[str, Any]]:
        with self._definition_lock:
            return self._definition_library.search(
                intent,
                limit=_AUTHOR_REFERENCE_LIMIT,
                source_format=SOURCE_FORMAT_PYTHON,
            )

    def attach_task_runner(self, task_runner: Any) -> None:
        """Register the TaskRunner start port for saved task-plan definitions."""
        self._task_runner = task_runner

    def get_definition(self, workflow_ref: str) -> Optional[dict[str, Any]]:
        with self._definition_lock:
            return self._definition_library.get(workflow_ref)

    def save_definition(
        self,
        source: str,
        *,
        name: str = "",
        description: str = "",
        slug: str = "",
        derived_from: Any = None,
        source_format: str = SOURCE_FORMAT_PYTHON,
    ) -> dict[str, Any]:
        """Validate and explicitly promote a source definition into the library."""
        with self._definition_lock:
            errors: list[str] = []
            meta: dict[str, Any] = {}
            if source_format == SOURCE_FORMAT_PYTHON:
                vr = validate(source)
                errors = list(vr.errors)
                meta = vr.meta or {}
            elif source_format == SOURCE_FORMAT_TASK_PLAN:
                try:
                    decompose_yaml(source)
                except (ImportError, ValueError, KeyError) as exc:
                    errors = [str(exc)]
            else:
                errors = [f"unsupported workflow source format: {source_format}"]
            if errors:
                return {"ok": False, "error": "; ".join(errors), "errors": errors}
            if derived_from is not None:
                definitions = self._definition_library.list()
                if not _lineage_exists(derived_from, definitions):
                    error = "workflow lineage does not resolve to a saved revision"
                    return {"ok": False, "error": error, "errors": [error]}
            try:
                definition = self._definition_library.create(
                    source=source,
                    name=name or str(meta.get("name", "")) or "workflow",
                    description=description or str(meta.get("description", "")),
                    slug=slug,
                    derived_from=derived_from,
                    source_format=source_format,
                )
            except SensitiveWorkflowSourceError as exc:
                return {"ok": False, "error": str(exc), "errors": [str(exc)]}
        return {"ok": True, "definition": definition}

    async def promote_run_definition(
        self,
        run_id: str,
        *,
        name: str = "",
        description: str = "",
        slug: str = "",
    ) -> dict[str, Any]:
        """Promote a reusable run source from its unredacted server snapshot."""
        handle = self.registry.get(run_id)
        if handle is None:
            return {"ok": False, "error": "no such workflow run", "not_found": True}
        if handle.workflow_id:
            return {
                "ok": False,
                "error": "saved workflow invocations are already reusable",
                "already_saved": True,
            }
        promotable_plan = handle.driver == "taskrunner" and handle.status == STATUS_PAUSED
        if handle.status != STATUS_FINISHED and not promotable_plan:
            return {
                "ok": False,
                "error": "workflow run is not finished",
                "not_finished": True,
            }
        if not handle.source_is_original:
            return {
                "ok": False,
                "error": "the original workflow source is no longer available",
                "source_not_original": True,
            }
        source = handle.source
        if not source:
            return {"ok": False, "error": "workflow run has no source", "no_source": True}
        if handle.source_format == SOURCE_FORMAT_PYTHON:
            validated = validate(source)
            if not validated.ok:
                return {
                    "ok": False,
                    "error": "; ".join(validated.errors),
                    "errors": validated.errors,
                }
            derived_from = _declared_lineage(validated.meta or {})
        elif handle.source_format == SOURCE_FORMAT_TASK_PLAN:
            try:
                decompose_yaml(source)
            except (ImportError, ValueError, KeyError) as exc:
                return {"ok": False, "error": str(exc), "errors": [str(exc)]}
            derived_from = (
                {
                    "workflow_id": handle.derived_from_workflow_id,
                    "revision": handle.derived_from_revision,
                }
                if handle.derived_from_workflow_id and handle.derived_from_revision
                else None
            )
        else:
            error = f"unsupported workflow source format: {handle.source_format}"
            return {"ok": False, "error": error, "errors": [error]}
        return await asyncio.to_thread(
            self.save_definition,
            source,
            name=name,
            description=description,
            slug=slug,
            derived_from=derived_from,
            source_format=handle.source_format,
        )

    def update_definition(
        self,
        workflow_id: str,
        *,
        source: str,
        expected_revision: int,
        name: Optional[str] = None,
        description: Optional[str] = None,
        slug: Optional[str] = None,
    ) -> dict[str, Any]:
        """Validate and append an optimistic-concurrency-controlled revision."""
        with self._definition_lock:
            current = self._definition_library.get(workflow_id)
            if current is None:
                return {
                    "ok": False,
                    "error": "no such saved workflow",
                    "not_found": True,
                }
            source_format = str(current.get("format") or SOURCE_FORMAT_PYTHON)
            if source_format == SOURCE_FORMAT_PYTHON:
                vr = validate(source)
                errors = list(vr.errors)
            elif source_format == SOURCE_FORMAT_TASK_PLAN:
                try:
                    decompose_yaml(source)
                    errors = []
                except (ImportError, ValueError, KeyError) as exc:
                    errors = [str(exc)]
            else:
                errors = [f"unsupported workflow source format: {source_format}"]
            if errors:
                return {"ok": False, "error": "; ".join(errors), "errors": errors}
            try:
                definition = self._definition_library.update(
                    str(current["id"]),
                    source=source,
                    expected_revision=expected_revision,
                    name=name,
                    description=description,
                    slug=slug,
                )
            except SensitiveWorkflowSourceError as exc:
                return {"ok": False, "error": str(exc), "errors": [str(exc)]}
        if definition is None:
            return {
                "ok": False,
                "error": "workflow definition has a newer revision",
                "conflict": True,
            }
        return {"ok": True, "definition": definition}

    async def start_definition(
        self,
        workflow_ref: str,
        *,
        input_text: str = "",
        args: Optional[dict[str, Any]] = None,
        author: str = "",
        session_key: str = "",
        budget_total: Optional[int] = None,
        timeout_secs: Optional[int] = None,
    ) -> dict[str, Any]:
        """Run the exact current revision of a named saved workflow."""
        definition = await asyncio.to_thread(self.get_definition, workflow_ref)
        if definition is None:
            return {"error": f"no such saved workflow: {workflow_ref}", "not_found": True}
        run_args = dict(args or {})
        effective_input = input_text or str(run_args.get("input", ""))
        if definition.get("format") == SOURCE_FORMAT_TASK_PLAN:
            if self._task_runner is None:
                return {
                    "error": "task runner is not available for this workflow",
                    "unavailable": True,
                }
            started = await self._task_runner.start_workflow_definition(
                definition,
                input_text=effective_input,
                author=author,
                session_key=session_key,
            )
            if "run_id" in started:
                started.update(
                    {
                        "workflow_id": definition["id"],
                        "slug": definition["slug"],
                        "revision": definition["revision"],
                    }
                )
            elif "error" in started:
                started["admission_rejected"] = True
            return started
        if input_text:
            run_args["input"] = input_text
        started = await self.start(
            str(definition["source"]),
            name=str(definition.get("name", "")),
            args=run_args,
            author=author,
            session_key=session_key,
            budget_total=budget_total,
            timeout_secs=timeout_secs,
            workflow_id=str(definition["id"]),
            workflow_slug=str(definition["slug"]),
            workflow_revision=int(definition["revision"]),
        )
        if "run_id" in started:
            started.update(
                {
                    "workflow_id": definition["id"],
                    "slug": definition["slug"],
                    "revision": definition["revision"],
                }
            )
        return started

    def status(self, run_id: str) -> Optional[dict]:
        return self.registry.status(run_id, include_events=False)

    def result(self, run_id: str) -> Optional[dict]:
        return self.registry.status(run_id, include_events=True)

    def list_runs(self) -> list[dict]:
        return self.registry.list()

    async def cancel(self, run_id: str) -> bool:
        return await self.registry.cancel(run_id)

    async def rerun_subtree(
        self,
        run_id: str,
        from_index: int = 0,
        *,
        source: Optional[str] = None,
        timeout_secs: Optional[int] = None,
    ) -> dict:
        """Re-run a prior workflow, replaying agent calls BEFORE ``from_index`` from
        cache and re-executing from there ("restart parts" at runtime).

        ``from_index`` is the agent ``call_index`` to restart at: calls 0..from_index-1
        reuse the prior run's cached results, calls >= from_index re-call the model.
        ``from_index=0`` re-runs everything fresh. Returns {run_id} or {error}.

        ``source`` (optional): an EDITED script to run instead of the prior run's
        stored source — the user can review the authored workflow, tweak it, and
        rerun. Editing the script can shift call indices, so the prefix-replay cache
        is NOT reused for an edited rerun (it runs fresh, ``replay_before=0``); the
        edited source is validated first and rejected with errors if invalid.
        """
        prior = self.registry.get(run_id)
        if prior is None:
            return {"error": f"no such run: {run_id}"}
        stripped = (source or "").strip()
        edited = bool(stripped and stripped != (prior.source or "").strip())
        run_source = source if stripped else prior.source
        if not run_source:
            return {"error": f"run {run_id} has no stored source to re-run"}
        if edited:
            vr = validate(run_source)
            if not vr.ok:
                return {"error": "; ".join(vr.errors), "errors": vr.errors}
        new_id = self._new_run_id()
        # An edited script can't safely replay the old prefix (call indices shift),
        # so force a fresh run; an unedited rerun keeps the replay cache.
        replay_before = 0 if edited else max(0, from_index)
        replay_results = {} if edited else dict(prior.agent_results)
        label = "rerun-edited" if edited else f"rerun@{from_index}"
        await self._runner(new_id, timeout_secs=timeout_secs).run_background(
            run_source,
            registry=self.registry,
            run_id=new_id,
            now=self._now_fn(),
            name=f"{prior.name} ({label})",
            args=prior.args,
            author=prior.author,
            session_key=prior.session_key,
            replay_results=replay_results,
            replay_before=replay_before,
            source_is_original=edited or prior.source_is_original,
            workflow_id="" if edited else prior.workflow_id,
            workflow_slug="" if edited else prior.workflow_slug,
            workflow_revision=0 if edited else prior.workflow_revision,
        )
        return {
            "run_id": new_id,
            "from": run_id,
            "replayed_before": replay_before,
            "edited": edited,
        }


def _strip_fence(text: str) -> str:
    """Strip a leading/trailing ``` fence if the model wrapped the script."""
    t = text.strip()
    if t.startswith("```"):
        # Peel ONLY the opening-fence line and the trailing fence — never split
        # on every ```, or a literal triple-backtick inside the script body
        # (e.g. ``return "```"``) would truncate the script mid-statement.
        if t.endswith("```"):
            t = t[:-3]
        newline = t.find("\n")
        if newline != -1:
            t = t[newline + 1 :]  # drop ``` / ```python / ```py opening line
        else:
            t = t[3:]  # single-line fence: drop just the opening ```
            if t.startswith("python"):
                t = t[len("python") :]
            elif t.startswith("py"):
                t = t[len("py") :]
    return t.strip() + "\n"


def _authoring_references(matches: list[dict[str, Any]]) -> str:
    if not matches:
        return (
            "No saved local workflow matched this intent. Author from scratch and "
            "do not add META.adapted_from."
        )
    blocks = [
        "Saved local workflows matched this intent. Adapt the closest one when useful; "
        "otherwise author from scratch. If you adapt one, add its exact reference as "
        'META["adapted_from"] = "<workflow-id>@<revision>". Never edit or overwrite the '
        "saved definition while authoring."
    ]
    for definition in matches:
        source = str(definition.get("source", ""))[:_AUTHOR_REFERENCE_SOURCE_CHARS]
        blocks.append(
            "\n--- saved workflow "
            f"{definition.get('id')}@{definition.get('revision')} "
            f"({definition.get('slug')}) ---\n{source}"
        )
    return "\n".join(blocks)


def _verified_lineage(
    meta: dict[str, Any], matches: list[dict[str, Any]]
) -> Optional[dict[str, Any]]:
    lineage = _declared_lineage(meta)
    if lineage is None:
        return None
    workflow_id = lineage["workflow_id"]
    revision = lineage["revision"]
    exists = any(
        definition.get("id") == workflow_id and definition.get("revision") == revision
        for definition in matches
    )
    return lineage if exists else None


def _declared_lineage(meta: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Parse a validated workflow's optional ``META.adapted_from`` reference."""
    reference = meta.get("adapted_from")
    if not isinstance(reference, str) or "@" not in reference:
        return None
    workflow_id, revision_text = reference.rsplit("@", 1)
    if not revision_text.isdigit():
        return None
    revision = int(revision_text)
    return {"workflow_id": workflow_id, "revision": revision}


def _lineage_exists(value: Any, definitions: list[dict[str, Any]]) -> bool:
    if not isinstance(value, dict):
        return False
    workflow_id = value.get("workflow_id")
    revision = value.get("revision")
    if (
        not isinstance(workflow_id, str)
        or not workflow_id
        or isinstance(revision, bool)
        or not isinstance(revision, int)
    ):
        return False
    for definition in definitions:
        if definition.get("id") != workflow_id:
            continue
        if definition.get("revision") == revision:
            return True
        revisions = definition.get("revisions", [])
        return any(
            isinstance(item, dict) and item.get("revision") == revision for item in revisions
        )
    return False
