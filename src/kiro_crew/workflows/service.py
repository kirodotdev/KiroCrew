"""Gateway-side workflow service: the shared registry + runner the chat tools,
the Workflows app, and result-to-chat injection all talk to (M6.3/M6.4/M6.5).

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
authoring-reliability gates (G1/G2) cover this shape.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Optional

from kiro_crew import autonudge
from kiro_crew.llm_helpers import ToolApprovalPolicy, stream_and_collect

from .agent_exec import build_agent_fn
from .agent_pool import build_pooled_agent_fn
from .registry import RunRegistry
from .runner import WorkflowRunner, clamp_run_timeout
from .store import WorkflowRunStore
from .validate import validate

logger = logging.getLogger(__name__)

# Bounded attempts to coax a valid script out of the model (mirrors schema retry).
_AUTHOR_RETRIES = 2

_AUTHOR_SYSTEM = """\
You are authoring a KiroCrew DYNAMIC WORKFLOW: one Python module that orchestrates
agents. Reply with ONLY the Python module (no prose, no code fence). It MUST be:

  META = {{"name": "<kebab-name>", "description": "<one line>", "phases": [...]}}
  async def workflow(ctx):
      ...
      return <json-serializable result>

Rules (the sandbox REJECTS violations): no imports; no open/eval/exec/__import__;
no dunder access; no time/random/uuid (use ctx.now / ctx.args). Use ONLY the ctx
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
    ) -> None:
        # Durable store (FIX-21): runs are mirrored to disk so they survive gateway
        # restarts. Pass persist=False (or store=None) to keep a purely in-memory
        # registry — used by tests that don't want filesystem side effects.
        if store is None and persist:
            try:
                store = WorkflowRunStore()
            except Exception:  # noqa: BLE001 - persistence is best-effort
                store = None
        self.registry = RunRegistry(store=store)
        if on_done is not None:
            self.registry.set_on_done(on_done)
        if on_event is not None:
            self.registry.set_on_event(on_event)
        self._sessions = sessions
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
            logger.warning(
                "workflow ctx.nudge skipped: session %r is not nudge-able", session_key
            )
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

        # M4 native ports wired for every run of this service. ``nudge`` bridges
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

                async def _teardown_pooled() -> None:
                    # Safety net (drain is a no-op if pre_terminal already ran;
                    # covers pre-exec failure paths), then release the pool.
                    await self._drain_nudge_tasks(run_id)
                    await pool.shutdown()

                return WorkflowRunner(
                    agent_fn=agent_fn,
                    concurrency=self._concurrency,
                    timeout_secs=ceiling,
                    ports=ports,
                    pre_terminal=_drain,
                    on_complete=_teardown_pooled,
                )
            except Exception:  # noqa: BLE001 - never let pooling break run start
                logger.warning(
                    "workflow agent pool init failed for %s; falling back to per-call sessions",
                    run_id,
                    exc_info=True,
                )
        agent_fn = build_agent_fn(self._sessions, run_id=run_id)

        async def _teardown() -> None:
            await self._drain_nudge_tasks(run_id)

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

        ``on_progress(msg)`` (M6.7) streams human-readable authoring progress (each
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
        # ACP backend injecting tools without set_mode. The session is torn down
        # (cleanup=True) the instant authoring finishes — nothing persists.
        key = f"wf-author:{self._new_run_id()}"
        provider, *_ = await self._sessions.get_or_create(key, agent="kirocrew-lite")
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
                prompt = _AUTHOR_SYSTEM.format(intent=intent)
                if errors:
                    prompt += f"\n\nYour previous script was INVALID: {'; '.join(errors)}. Fix it."
                text = await stream_and_collect(
                    provider, prompt, approval_policy=ToolApprovalPolicy.REJECT_ALL
                )
                source = _strip_fence(text)
                vr = validate(source)
                if vr.ok:
                    _say(f"Script validated: {(vr.meta or {}).get('name', 'workflow')}")
                    return {"ok": True, "source": source, "meta": vr.meta}
                errors = vr.errors
            return {"ok": False, "errors": errors, "source": source}
        finally:
            # Ephemeral, isolated session: tear it down so nothing persists between
            # runs (separation of concerns — the workflow is independent).
            try:
                self._sessions.release(key, cleanup=True)
            except Exception:  # noqa: BLE001
                pass

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
        """Launch a background run that AUTHORS its own script from ``intent`` (M6.7).

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
        )
        return {"run_id": run_id, "name": name or (vr.meta or {}).get("name", "")}

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
        cache and re-executing from there ("restart parts" at runtime, M6.6).

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
        edited = bool(
            source is not None and source.strip() and source.strip() != (prior.source or "").strip()
        )
        run_source = source if (source is not None and source.strip()) else prior.source
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
