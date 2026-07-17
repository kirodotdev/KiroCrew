"""WorkflowRunner — execute a validated workflow script with ceilings + event stream.

Top of the layering (``dsl`` → ``context`` → ``runner``; GATE F1). Ties together:
validation (``validate``), the restricted namespace + ceilings (``context``), the
scheduling combinators (``dsl``), and the event stream (``events``).

Gates closed here:

* A7 — emits the full documented event stream (``run_started`` … ``run_finished`` /
  ``run_failed`` / ``run_cancelled``), in order, via ``EventStream``.
* B5 — a wall-clock timeout terminates a runaway script (``asyncio.wait_for``).
* (consumes A4/B6 from ``context``: ``Budget`` ceiling, ``AgentCounter`` cap.)

Agent execution is injected as ``agent_fn`` so the runner is testable against a
stub now; the real wiring (subagent-by-default via ``SubagentManager``, ``session=``
via ``SessionManager``) drops in later WITHOUT changing the frozen ``ctx`` contract.
The runner never spawns real ``kiro-cli`` — that is the caller's ``agent_fn``.

``now`` is a fixed run-start stamp supplied by the caller (NOT ``time`` inside the
script's scope) so the stream stays deterministic / resume-stable. ``time`` is used
here in the RUNNER (host code), only for the wall-clock guard and duration — never
exposed to the script.

Spec: ``docs/system-specs/modules/workflows.md``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from . import BudgetExceeded, WorkflowEvent
from .context import DEFAULT_MAX_AGENTS_PER_RUN, AgentCounter, Budget, build_safe_globals
from .dsl import parallel as _parallel
from .dsl import pipeline as _pipeline
from .events import EventStream
from .registry import (
    STATUS_CANCELLED,
    STATUS_FAILED,
    STATUS_FINISHED,
    start_background_run,
)
from .schema import run_with_schema
from .validate import validate

# Optional dependency (gate F1): the SEL security event log lives in the app
# layer, and the workflows engine must stay importable as a standalone unit
# without it. Resolved at module top via try/except so the top-level-imports rule
# is satisfied; ``None`` when SEL is unavailable, in which case _default_audit is
# a no-op. The audit sink is also injectable, so only the DEFAULT sink touches SEL.
try:
    from kiro_crew.sel import sel as _sel
except ImportError:  # pragma: no cover - SEL is app-layer optional for the engine
    _sel = None  # type: ignore[assignment]

# Wall-clock ceiling per run (matches ``_RUN_TIMEOUT_SECS`` in the spec).
DEFAULT_RUN_TIMEOUT_SECS = 3600

# Signature of the injected agent executor: (prompt, options) -> result string/dict.
AgentFn = Callable[[str, dict], Awaitable[Any]]

# Signature of the injected SEL audit sink (GATE B10). Defaults to the real
# ``kiro_crew.sel`` security event log; tests inject a capturing stub. Kept as a
# thin callable so the engine has no hard import of dashboard/security internals
# beyond the audit boundary, and so audit failures can never break a run.
#   audit(event_type, *, run_id, fields: dict) -> None
AuditFn = Callable[..., None]


def _default_audit(event_type: str, *, run_id: str, fields: dict) -> None:
    """Write a workflow audit record to the SEL security event log (B10).

    ``_sel`` is the optional app-layer SEL accessor resolved at module top (gate
    F1); when it's None (standalone engine without the app), this is a no-op.
    """
    if _sel is None:
        return
    try:
        _sel().log_tool_invocation(
            session_key=fields.get("runner", "") or run_id,
            source="workflow",
            tool_name=f"workflow.{event_type}",
            tool_kind="workflow",
            outcome=fields.get("outcome", "ok"),
            request_id=run_id,
            metadata=fields,
        )
    except Exception:  # noqa: BLE001 - audit must never break a run
        pass


def _guarded_audit(audit: AuditFn) -> AuditFn:
    """Wrap any audit sink so a raising sink can never break a run.

    ``_default_audit`` guards itself, but an *injected* sink (tests, alternate
    SEL backends) may raise. Wrapping at assignment makes every call site safe
    without a try/except at each one, so the documented invariant — "a broken
    audit sink must not fail the run" — holds for arbitrary injected sinks too.
    """

    def _audit(event_type: str, *, run_id: str, fields: dict) -> None:
        try:
            audit(event_type, run_id=run_id, fields=fields)
        except Exception:  # noqa: BLE001 - audit must never break a run
            pass

    return _audit


def _result_hash(value: Any) -> str:
    """Stable short hash of a run result for the audit trail (never the raw data)."""
    try:
        blob = json.dumps(value, sort_keys=True, default=str)
    except Exception:  # noqa: BLE001
        blob = repr(value)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


@dataclass
class RunResult:
    """Outcome of a workflow run: the script's return value + the full event stream."""

    run_id: str
    ok: bool
    result: Any
    events: list[WorkflowEvent]
    error: Optional[str] = None
    # Per-agent-call results (call_index → result), so a resume/restart-subtree
    # can replay the unchanged prefix (M6.6).
    agent_results: dict = field(default_factory=dict)
    # The script actually executed. Equals the input ``source`` unless the run
    # authored it from an ``intent`` (M6.7) — surfaced so a background run can
    # store the authored script on its handle for rerun/restart.
    source: str = ""


# Signature of the injected authoring step (M6.7): intent -> {ok, source, errors}.
# Kept as a narrow injected callable (NOT a hard import of the service) so the
# runner stays at the top of the layering and authoring uses the host's model
# plumbing. ``on_progress(msg)`` lets authoring stream human-readable progress.
AuthorFn = Callable[..., Awaitable[dict]]


class _NoOpContextManager:
    """Lightweight sync context manager that does nothing.

    Returned by ctx.log() and ctx.nudge() so ``with ctx.log(...):`` doesn't crash
    even though those methods have no meaningful enter/exit semantics.
    """

    def __enter__(self) -> "_NoOpContextManager":
        return self

    def __exit__(self, *_: object) -> None:
        return None


class _PhaseContextManager(_NoOpContextManager):
    """Sync context manager returned by ctx.phase() — supports both bare-call and
    ``with ctx.phase("title"):`` patterns that LLMs naturally generate."""


class _RunContext:
    """Concrete ``WorkflowContext`` assembled per run (satisfies the frozen Protocol).

    Wires ``dsl.parallel/pipeline`` + ``Budget`` + ``AgentCounter`` + ``EventStream``.
    KiroCrew-native ports (cron/memory/learn/knowledge) are None until M4; ``agent``
    delegates to the injected ``agent_fn`` (stub in tests, real spawner in prod).
    """

    def __init__(
        self,
        *,
        run_id: str,
        args: dict,
        now: str,
        owner_dm: str,
        stream: EventStream,
        budget: Budget,
        counter: AgentCounter,
        agent_fn: AgentFn,
        concurrency: Optional[int],
        author: str = "",
        runner: str = "",
        audit: Optional[AuditFn] = None,
        ports: Optional[dict] = None,
        on_event: Optional[Callable[[WorkflowEvent], None]] = None,
        replay_results: Optional[dict] = None,
        replay_before: int = 0,
    ) -> None:
        self.args = args
        self.now = now
        self.owner_dm = owner_dm
        self.budget = budget

        # KiroCrew-native ports (M4) — injected per run; None when the host did
        # not grant/wire them (the frozen contract allows None, like AppContext).
        ports = ports or {}
        self.cron = ports.get("cron")
        self.memory = ports.get("memory")
        self.learn = ports.get("learn")
        self.knowledge = ports.get("knowledge")
        self._nudge_fn = ports.get("nudge")
        self._approve_fn = ports.get("approve")
        self._send_slack_fn = ports.get("send_slack")
        self._send_message_fn = ports.get("send_message")

        self._run_id = run_id
        self._author = author
        self._runner = runner
        self._audit = _guarded_audit(audit or _default_audit)
        self._stream = stream
        self._counter = counter
        self._agent_fn = agent_fn
        self._concurrency = concurrency
        self._current_phase = ""
        self._events: list[WorkflowEvent] = []
        self._on_event = on_event
        # Resume / restart-subtree (M6.6): cached agent results from a prior run,
        # replayed for call_index < replay_before; calls at/after re-execute live.
        # ``agent_results`` collects THIS run's results for the next resume.
        self._replay_results: dict[int, Any] = replay_results or {}
        self._replay_before = replay_before
        self.agent_results: dict[int, Any] = {}

    # --- event sink (shared with the runner) ---
    def _record(self, event: WorkflowEvent) -> None:
        self._events.append(event)
        # Live fan-out (M6): the registry / WS push consumes events as they happen.
        if self._on_event is not None:
            try:
                self._on_event(event)
            except Exception:  # noqa: BLE001 - a bad subscriber must not break the run
                pass

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
    ) -> Any:
        # B6 cap + A4 ceiling are checked BEFORE the call so a script cannot run
        # past either limit. would_exceed lets us stop at the boundary cleanly.
        self._counter.increment()
        if self.budget.would_exceed():
            raise BudgetExceeded("budget exhausted before agent call")

        call_index = self._counter.count - 1
        agent_id = f"a{call_index}"
        use_phase = phase or self._current_phase
        self._record(
            self._stream.agent_started(
                self.now,
                agent_id=agent_id,
                label=label or prompt[:40],
                phase=use_phase,
                call_index=call_index,
            )
        )
        opts = {
            "label": label,
            "phase": use_phase,
            "schema": schema,
            "model": model,
            "agent": agent,
            "effort": effort,
            "cwd": cwd,
            "session": session,
            "nudge": nudge,
        }
        try:
            if call_index < self._replay_before and call_index in self._replay_results:
                # Resume (M6.6): replay the cached result from the prior run instead
                # of re-calling the model. Determinism (no time/random + stable
                # call_index) makes this sound — same script+args ⇒ same call order.
                result = self._replay_results[call_index]
                ok = result is not None
            elif schema is not None:
                # Structured output (C1–C3): re-ask until the model yields
                # schema-valid JSON, or None after bounded retries. The producer
                # is the same injected agent_fn (so prod/stub both flow through).
                async def _produce(p: str) -> str:
                    out = await self._agent_fn(p, opts)
                    return out if isinstance(out, str) else json.dumps(out)

                result = await run_with_schema(_produce, prompt, schema)
                ok = result is not None
            else:
                result = await self._agent_fn(prompt, opts)
                ok = result is not None
        except BudgetExceeded:
            raise
        except Exception:
            result, ok = None, False
        # Record this call's result so a future resume can replay the prefix.
        self.agent_results[call_index] = result
        self._record(
            self._stream.agent_finished(
                self.now,
                agent_id=agent_id,
                result_summary=("" if result is None else str(result)[:120]),
                ok=ok,
            )
        )
        # B10: audit each agent call (author/runner/args carried at run level).
        self._audit(
            "agent_call",
            run_id=self._run_id,
            fields={
                "author": self._author,
                "runner": self._runner,
                "agent_id": agent_id,
                "call_index": call_index,
                "outcome": "ok" if ok else "failed",
                "has_schema": schema is not None,
            },
        )
        return result

    # --- scheduling (delegate to the dsl combinators with the run's cap) ---
    async def parallel(self, thunks: list) -> list:
        return await _parallel(thunks, limit=self._concurrency)

    async def pipeline(self, items: list, *stages: Callable) -> list:
        return await _pipeline(items, *stages, limit=self._concurrency)

    async def workflow(self, name: str, args: Optional[dict] = None) -> Any:
        # Nested workflow execution lands with the registry (M5); contract present now.
        raise NotImplementedError("nested ctx.workflow() arrives with the registry (M5)")

    # --- progress / UI ---
    def phase(self, title: str) -> "_PhaseContextManager":
        """Set the current phase and emit a phase_started event.

        Returns a stateless context manager so BOTH calling styles work:
          ctx.phase("read")           # bare call — original pattern
          with ctx.phase("read"):     # context-manager — purely cosmetic grouping
              ...

        The CM is stateless: __exit__ does NOT end the phase or restore the
        previous one.  The phase persists until the next ctx.phase() call.
        """
        self._current_phase = title
        self._record(self._stream.phase_started(self.now, title=title))
        return _PhaseContextManager()

    def log(self, message: str) -> "_NoOpContextManager":
        """Log a message to the event stream.

        Returns a no-op context manager so ``with ctx.log(...):`` doesn't crash,
        even though ``ctx.log("x")`` bare-call is the intended pattern.
        """
        self._record(self._stream.log(self.now, message=message))
        return _NoOpContextManager()

    # --- KiroCrew-native (M4): delegate to injected port fns; clear error if a
    #     workflow uses a primitive the host did not wire/permit for this run. ---
    def nudge(self, *, idle_secs: int, message: str, max_cycles: int = 0) -> "_NoOpContextManager":
        if self._nudge_fn is None:
            raise RuntimeError("ctx.nudge is not available for this run (no nudge port wired)")
        self._nudge_fn(idle_secs=idle_secs, message=message, max_cycles=max_cycles)
        return _NoOpContextManager()

    async def approve(self, prompt: str) -> bool:
        if self._approve_fn is None:
            raise RuntimeError("ctx.approve is not available for this run (no approve port wired)")
        return await self._approve_fn(prompt)

    async def send_slack(self, target: str, text: str) -> None:
        if self._send_slack_fn is None:
            raise RuntimeError("ctx.send_slack is not available for this run (no slack port wired)")
        await self._send_slack_fn(target, text)

    async def send_message(self, channel: str, text: str) -> None:
        if self._send_message_fn is None:
            raise RuntimeError("ctx.send_message is not available (no message port wired)")
        await self._send_message_fn(channel, text)


class WorkflowRunner:
    """Validates, executes, and streams events for one workflow script.

    ``agent_fn`` is the injected agent executor (stub in tests). ``timeout_secs``
    is the B5 wall-clock ceiling. ``concurrency`` bounds ``parallel``/``pipeline``
    fan-out (the caller passes ``resolve_max_subagents()`` in prod).
    """

    def __init__(
        self,
        *,
        agent_fn: AgentFn,
        timeout_secs: int = DEFAULT_RUN_TIMEOUT_SECS,
        max_agents_per_run: int = DEFAULT_MAX_AGENTS_PER_RUN,
        concurrency: Optional[int] = None,
        audit: Optional[AuditFn] = None,
        ports: Optional[dict] = None,
    ) -> None:
        self._agent_fn = agent_fn
        self._timeout_secs = timeout_secs
        self._max_agents = max_agents_per_run
        self._concurrency = concurrency
        # B10 audit sink (default = real SEL) + M4 native ports (default = none wired).
        self._audit = _guarded_audit(audit or _default_audit)
        self._ports = ports or {}

    async def run(
        self,
        source: str,
        *,
        run_id: str,
        now: str,
        args: Optional[dict] = None,
        owner_dm: str = "",
        budget_total: Optional[int] = None,
        script_hash: str = "",
        author: str = "",
        on_event: Optional[Callable[[WorkflowEvent], None]] = None,
        replay_results: Optional[dict] = None,
        replay_before: int = 0,
        intent: str = "",
        author_fn: Optional[AuthorFn] = None,
        on_source: Optional[Callable[[str], None]] = None,
    ) -> RunResult:
        """Execute a workflow script end-to-end, returning result + event stream.

        ``on_event`` (M6) is fired for every event as it is produced — lifecycle
        (run_started/finished/…) AND in-script (phase/log/agent) — so a background
        registry / WS push can monitor the run live. It must never raise.

        ``replay_results`` + ``replay_before`` (M6.6) drive resume / restart-subtree:
        agent calls with ``call_index < replay_before`` reuse the cached prior
        result instead of re-calling the model; calls at/after re-execute live.

        ``intent`` + ``author_fn`` (M6.7) author the script *inside the run* when no
        ``source`` is given: authoring becomes a visible "Authoring" phase whose
        progress streams to ``on_event`` (sidebar + chat) — so ``workflow_run`` can
        return a run_id instantly instead of blocking on a slow synchronous author.
        """
        args = args or {}
        stream = EventStream(run_id)
        events: list[WorkflowEvent] = []

        def emit(ev: WorkflowEvent) -> WorkflowEvent:
            """Append a lifecycle event AND fan it out to the live subscriber."""
            events.append(ev)
            if on_event is not None:
                try:
                    on_event(ev)
                except Exception:  # noqa: BLE001 - subscriber must not break the run
                    pass
            return ev

        # B10: record run start (author/runner/args) up front, before any exec.
        self._audit(
            "run_started",
            run_id=run_id,
            fields={
                "author": author,
                "runner": owner_dm or run_id,
                "arg_keys": sorted(args.keys()),
                "script_hash": script_hash,
                "outcome": "started",
            },
        )

        # 0. Author-in-run (M6.7): if we were handed an intent and no source, turn
        # the intent into a validated script HERE, as a visible "Authoring" phase.
        # This is why workflow_run(intent=…) can return a run_id instantly: the slow
        # model call(s) happen in the background run, streaming progress, not behind
        # a 30s synchronous HTTP author. We emit run_started first so the run shows
        # up live the instant it is scheduled.
        if not source and intent and author_fn is not None:
            emit(
                stream.run_started(
                    now, name="", args=args, script_hash=script_hash, budget_total=budget_total
                )
            )
            emit(stream.phase_started(now, title="Authoring"))
            emit(stream.log(now, message="Authoring workflow from your request…"))

            def _auth_progress(msg: str) -> None:
                emit(stream.log(now, message=msg))

            try:
                authored = await author_fn(intent, on_progress=_auth_progress)
            except Exception as exc:  # noqa: BLE001 - authoring failure → failed run
                emit(stream.run_failed(now, error=f"authoring error: {exc!r}", where="author"))
                return RunResult(
                    run_id, ok=False, result=None, events=events, error=f"authoring: {exc!r}"
                )
            if not authored.get("ok"):
                errs = "; ".join(authored.get("errors", []) or ["could not author a valid script"])
                emit(stream.log(now, message=f"Authoring failed: {errs}"))
                emit(stream.run_failed(now, error=errs, where="author"))
                return RunResult(run_id, ok=False, result=None, events=events, error=errs)
            source = authored.get("source", "")
            # Publish the authored script to the handle NOW (mid-run), so "View
            # source" works while the run is still executing — not only after it
            # finishes. Best-effort; a bad subscriber must not break the run.
            if on_source is not None and source:
                try:
                    on_source(source)
                except Exception:  # noqa: BLE001
                    pass
            emit(stream.log(now, message="Workflow authored — starting execution."))
            # Authored source is validated by the author step; defend anyway and
            # run_started was already emitted, so go straight to exec.
            vr = validate(source)
            if not vr.ok:
                emit(stream.run_failed(now, error="; ".join(vr.errors), where="validate"))
                return RunResult(
                    run_id, ok=False, result=None, events=events,
                    error="; ".join(vr.errors), source=source,
                )
            return await self._exec_validated(
                source, run_id=run_id, now=now, args=args, owner_dm=owner_dm,
                budget_total=budget_total, author=author, on_event=on_event,
                replay_results=replay_results, replay_before=replay_before,
                stream=stream, events=events, emit=emit,
            )

        # 1. Validate (B-group static). A bad script fails before any exec.
        vr = validate(source)
        if not vr.ok:
            emit(
                stream.run_started(
                    now, name="", args=args, script_hash=script_hash, budget_total=budget_total
                )
            )
            emit(stream.run_failed(now, error="; ".join(vr.errors), where="validate"))
            return RunResult(
                run_id, ok=False, result=None, events=events, error="; ".join(vr.errors)
            )

        name = (vr.meta or {}).get("name", "")
        emit(
            stream.run_started(
                now, name=name, args=args, script_hash=script_hash, budget_total=budget_total
            )
        )
        return await self._exec_validated(
            source, run_id=run_id, now=now, args=args, owner_dm=owner_dm,
            budget_total=budget_total, author=author, on_event=on_event,
            replay_results=replay_results, replay_before=replay_before,
            stream=stream, events=events, emit=emit,
        )

    async def _exec_validated(
        self,
        source: str,
        *,
        run_id: str,
        now: str,
        args: dict,
        owner_dm: str,
        budget_total: Optional[int],
        author: str,
        on_event: Optional[Callable[[WorkflowEvent], None]],
        replay_results: Optional[dict],
        replay_before: int,
        stream: EventStream,
        events: list[WorkflowEvent],
        emit: Callable[[WorkflowEvent], WorkflowEvent],
    ) -> RunResult:
        """Build the run context, exec the (already validated) script under the
        wall-clock guard, and emit the terminal event. Shared by the source-given
        and the author-in-run paths; assumes ``run_started`` was already emitted.
        """
        # Defense-in-depth: re-validate before exec so a future refactor that
        # bypasses the caller's validate() step cannot reach exec unchecked.
        # This is cheap (AST parse, no I/O) and provides the hard invariant
        # that CodeQL/Semgrep look for at the exec site.
        vr = validate(source)
        if not vr.ok:
            emit(stream.run_failed(now, error="; ".join(vr.errors), where="validate"))
            return RunResult(
                run_id, ok=False, result=None, events=events,
                error="; ".join(vr.errors), source=source,
            )

        # 2. Build the run context + restricted exec namespace.
        ctx = _RunContext(
            run_id=run_id,
            args=args,
            now=now,
            owner_dm=owner_dm,
            stream=stream,
            budget=Budget(total=budget_total),
            counter=AgentCounter(self._max_agents),
            agent_fn=self._agent_fn,
            concurrency=self._concurrency,
            author=author,
            runner=owner_dm or run_id,
            audit=self._audit,
            ports=self._ports,
            on_event=on_event,
            replay_results=replay_results,
            replay_before=replay_before,
        )
        ctx._events = events  # share the sink so phase/log/agent events land in order
        safe_globals = build_safe_globals(ctx)

        # 3. exec the module (defines `workflow`) then await it under a wall clock.
        started = time.monotonic()
        task: Optional["asyncio.Task[Any]"] = None
        try:
            exec(compile(source, f"<workflow:{run_id}>", "exec"), safe_globals)  # noqa: S102  # nosemgrep: python.lang.security.audit.exec-detected.exec-detected
            entry = safe_globals.get("workflow")
            if entry is None:
                raise RuntimeError("script defines no 'workflow' coroutine")
            # B5 wall-clock guard. We deliberately do NOT use ``asyncio.wait_for``:
            # on CPython 3.10 it can leak the inner ``CancelledError`` to the caller
            # when the timeout races task completion (reproducible under coverage
            # instrumentation and on a loaded fleet under 16-worker xdist). With
            # ``asyncio.wait(timeout=)`` the loop never cancels FOR us — the runner
            # owns the cancellation and always converts a timeout into a clean
            # ``run_failed`` (where="ceiling") instead of propagating cancellation.
            run_task: "asyncio.Task[Any]" = asyncio.ensure_future(entry(ctx))
            task = run_task  # keep an outer ref for the cancel handler below
            done, _pending = await asyncio.wait({run_task}, timeout=self._timeout_secs)
            if run_task not in done:
                # Runaway: cancel it and drain its cancellation quietly so no
                # CancelledError escapes and no "task was destroyed" warning fires.
                run_task.cancel()
                try:
                    await run_task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
                emit(
                    stream.run_failed(
                        now, error=f"run exceeded {self._timeout_secs}s", where="ceiling"
                    )
                )
                return RunResult(
                    run_id, ok=False, result=None, events=events, error="timeout", source=source
                )
            result = run_task.result()  # re-raises the script's own exception, if any
        except asyncio.CancelledError:
            # The RUN itself was cancelled by our caller (not a timeout) — stop the
            # in-flight script and report it as cancelled.
            if task is not None and not task.done():
                task.cancel()
            emit(stream.run_cancelled(now, reason="cancelled"))
            return RunResult(
                run_id, ok=False, result=None, events=events, error="cancelled", source=source
            )
        except BudgetExceeded as exc:
            emit(stream.run_failed(now, error=str(exc), where="ceiling"))
            return RunResult(
                run_id, ok=False, result=None, events=events, error=str(exc), source=source
            )
        except Exception as exc:  # script raised — captured, not propagated
            emit(stream.run_failed(now, error=repr(exc), where="exec"))
            return RunResult(
                run_id, ok=False, result=None, events=events, error=repr(exc), source=source
            )

        duration = time.monotonic() - started
        emit(stream.run_finished(now, result=result, duration_s=duration))
        # B10: record successful completion with a result hash (never the raw data).
        self._audit(
            "run_finished",
            run_id=run_id,
            fields={
                "author": author,
                "runner": owner_dm or run_id,
                "outcome": "ok",
                "result_hash": _result_hash(result),
                "agent_calls": ctx._counter.count,
            },
        )
        return RunResult(
            run_id, ok=True, result=result, events=events,
            agent_results=dict(ctx.agent_results), source=source,
        )

    async def run_background(
        self,
        source: str,
        *,
        registry: "Any",
        run_id: str,
        now: str,
        name: str = "",
        args: Optional[dict] = None,
        owner_dm: str = "",
        session_key: str = "",
        budget_total: Optional[int] = None,
        script_hash: str = "",
        author: str = "",
        replay_results: Optional[dict] = None,
        replay_before: int = 0,
        intent: str = "",
        author_fn: Optional[AuthorFn] = None,
    ) -> str:
        """Start this workflow as a BACKGROUND run tracked in ``registry`` (M6).

        Returns the ``run_id`` immediately; the run drives on the event loop and
        streams its events into the registry handle (so chat MCP tools and the
        Workflows tab can monitor/cancel it by id). On terminal state the registry
        fires its ``on_done`` (M6.4 result-to-chat injection).

        ``replay_results``/``replay_before`` (M6.6) let a restart-subtree re-run
        replay the unchanged prefix from a prior run's cached agent results.

        ``intent``/``author_fn`` (M6.7): when ``source`` is empty, the script is
        authored *inside* the run (a visible "Authoring" phase) so this returns a
        run_id instantly instead of blocking on a slow synchronous author. The
        authored script is written back onto the handle for rerun/restart.
        """
        def _publish_source(src: str) -> None:
            # Write the authored script onto the handle the instant authoring
            # completes (mid-run), so "View source" works during execution.
            h = registry.get(run_id)
            if h is not None and src:
                h.source = src
                # Durably checkpoint the script now (FIX-21) — so an authored-in-run
                # workflow's source survives a restart even before it finishes.
                persist = getattr(registry, "persist", None)
                if persist is not None:
                    try:
                        persist(run_id)
                    except Exception:  # noqa: BLE001
                        pass

        async def _factory(
            record: Callable[[WorkflowEvent], None],
        ) -> tuple[Any, str, Optional[str], dict]:
            res = await self.run(
                source,
                run_id=run_id,
                now=now,
                args=args,
                owner_dm=owner_dm,
                budget_total=budget_total,
                script_hash=script_hash,
                author=author,
                on_event=record,
                replay_results=replay_results,
                replay_before=replay_before,
                intent=intent,
                author_fn=author_fn,
                on_source=_publish_source,
            )
            # Belt-and-suspenders: ensure the final source is on the handle even if
            # the mid-run publish was skipped (e.g. pre-authored source path).
            h = registry.get(run_id)
            if h is not None and res.source:
                h.source = res.source
            # run() captures its own CancelledError and returns error="cancelled"
            # (rather than re-raising) — map that to the cancelled terminal state
            # so the registry reflects a user cancel, not a generic failure.
            if res.ok:
                status = STATUS_FINISHED
            elif res.error == "cancelled":
                status = STATUS_CANCELLED
            else:
                status = STATUS_FAILED
            return res.result, status, res.error, res.agent_results

        return await start_background_run(
            registry,
            _factory,
            run_id=run_id,
            name=name,
            author=author,
            session_key=session_key,
            source=source,
            args=args or {},
        )
