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

from typing import Any, Callable, Optional

from kiro_crew.llm_helpers import ToolApprovalPolicy, stream_and_collect

from .agent_exec import build_agent_fn
from .registry import RunRegistry
from .runner import WorkflowRunner
from .store import WorkflowRunStore
from .validate import validate

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
await ctx.pipeline(items, *stages), await ctx.workflow(name, args?), ctx.phase(t),
ctx.log(m), ctx.nudge(idle_secs=?, message=?), ctx.budget, ctx.args, and (if
available) ctx.cron/ctx.memory/ctx.learn/await ctx.approve/await ctx.send_slack/
await ctx.send_message.

ASYNC vs SYNC — get this right or the script crashes at runtime:
  * AWAIT these (they are async): ctx.agent(...), ctx.parallel(...),
    ctx.pipeline(...), ctx.workflow(...), ctx.approve(...), ctx.send_slack(...),
    ctx.send_message(...).
  * Do NOT await these (they are SYNCHRONOUS and return None): ctx.phase(title),
    ctx.log(msg), ctx.nudge(...). Write ``ctx.phase("read")`` — NEVER
    ``await ctx.phase("read")`` (awaiting None raises TypeError immediately).

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
ctx.agent/parallel/pipeline/workflow/approve result that is not first bound to a
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
        self._now_fn = now_fn or (lambda: "1970-01-01T00:00:00Z")
        self._concurrency = concurrency
        self._seq = 0
        # Rehydrate any persisted runs from a prior process, and continue the
        # run-id sequence past the highest seen so new ids never collide.
        try:
            n = self.registry.load_persisted()
            if n:
                self._seq = self._max_persisted_seq()
        except Exception:  # noqa: BLE001
            pass

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

    def _runner(self, run_id: str) -> WorkflowRunner:
        agent_fn = build_agent_fn(self._sessions, run_id=run_id)
        return WorkflowRunner(agent_fn=agent_fn, concurrency=self._concurrency)

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
        # uses the tool-less ``meshclaw-lite`` agent. That is the lever that makes a
        # fresh session cheap: the dominant cold-start cost was loading the full
        # MCP toolset + system prompt; lite carries no tools, so the turn is just
        # the generation. REJECT_ALL is belt-and-suspenders against the Claude Code
        # backend injecting tools without set_mode. The session is torn down
        # (cleanup=True) the instant authoring finishes — nothing persists.
        key = f"wf-author:{self._new_run_id()}"
        provider, *_ = await self._sessions.get_or_create(key, agent="meshclaw-lite")
        try:
            errors: list[str] = []
            source = ""
            attempts = _AUTHOR_RETRIES + 1
            for i in range(attempts):
                if errors:
                    _say(f"Script was invalid ({'; '.join(errors)}) — revising (attempt {i + 1}/{attempts})…")
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
    ) -> dict:
        """Launch a background run that AUTHORS its own script from ``intent`` (M6.7).

        Returns ``{run_id}`` immediately — authoring happens inside the run as a
        visible "Authoring" phase, so the slow model call(s) never block this call
        (no more 30s synchronous-author timeout) and progress streams to the UI.
        """
        if not intent.strip():
            return {"error": "intent is required"}
        run_id = self._new_run_id()

        async def _author_fn(it: str, *, on_progress: Optional[Callable[[str], None]] = None) -> dict:
            return await self.author(it, author=author, on_progress=on_progress)

        await self._runner(run_id).run_background(
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
    ) -> dict:
        """Validate + launch a background run; return {run_id} or {error}."""
        vr = validate(source)
        if not vr.ok:
            return {"error": "; ".join(vr.errors), "errors": vr.errors}
        run_id = self._new_run_id()
        await self._runner(run_id).run_background(
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
        self, run_id: str, from_index: int = 0, *, source: Optional[str] = None
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
        await self._runner(new_id).run_background(
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
            "run_id": new_id, "from": run_id,
            "replayed_before": replay_before, "edited": edited,
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
