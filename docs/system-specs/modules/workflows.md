# Dynamic Workflows Module

## Overview

A **dynamic workflow** is one LLM-authored Python module that orchestrates several
Kiro Crew agents through ordered phases. The module declares a pure-literal `META`
dict and a single `async def workflow(ctx)` entrypoint; the engine validates it
statically, executes it in a restricted namespace under hard ceilings, and streams
a typed event journal that drives the UI, the chat `workflow_*` MCP tools, and
resume.

This module also provides Kiro Crew's shared orchestration substrate: run
identity, lifecycle state, event history, exact source, provenance, cancellation
binding, and the reusable definition library. `RunRegistry.has_pending_work_for`
provides a session-scoped, in-memory execution/terminal-handoff query for
conversation-completion notifications. A terminal run whose driver has not
exited still owns its handoff; no event or result snapshot is built for this
query. Python dynamic workflows are one
driver of that substrate. TaskRunner is another, stricter product layer: it uses
the common substrate but keeps its planning, approval, retry, test,
git/worktree, replan, persistence, and cleanup semantics.

The subsystem lives in `src/kiro_crew/workflows/` (13 modules). This file is the
**frozen contract** those modules cite: `workflows/__init__.py` declares the `ctx`
Protocol and the event vocabulary and points here, `events.py` says the per-type
`data` field table lives here, and `validate.py` says never to relax a check here
without updating the invariant tests. See "Changing this contract" at the end.

Runnable illustrative scripts: [`examples/workflows/`](examples/workflows/README.md).

## Interface-first freeze

`workflows/__init__.py` is **deliberately implementation-free**: it declares only
Protocols, one exception class, the `EVENT_TYPES` tuple, and the `WorkflowEvent`
dataclass. Its docstring states that importing it must have **no side effects**,
which is what lets every other module (and the conformance test) import the
contract without pulling in the gateway, a session manager, or a model.

`__all__` is the public freeze and is asserted exactly by
`test/test_workflows_conformance.py::test_all_exports_exact`:

```
WorkflowContext  Budget  CronPort  MemoryPort  LearnPort  KnowledgePort
BudgetExceeded   WorkflowEvent  EVENT_TYPES  AgentResult  Thunk
```

Two type aliases carry meaning:

| Alias | Definition | Meaning |
|-------|-----------|---------|
| `Thunk` | `Callable[[], Any]` | zero-arg callable returning an awaitable, the documented form for `parallel()` |
| `AgentResult` | `Union[str, dict, None]` | what `ctx.agent()` resolves to: validated dict (with `schema=`), free text, or `None` |

### Module layering

`test/test_workflows_architecture.py` (a stdlib AST scan) enforces the allowed
intra-package imports per module, so the direction cannot drift:

```
__init__, validate, dsl, schema, events, registry, store,
agent_exec, agent_pool, preview   (leaves: no sibling imports, or __init__ only)
library                         (definition persistence; may import store)
    ↑
context      (may import: __init__, validate)
    ↑
runner       (may import: __init__, validate, dsl, events, context, schema, registry)
    ↑
service      (may import: validate, registry, runner, agent_exec, agent_pool, store, library)
```

The same test forbids the engine from importing `kiro_crew.dashboard.state` or
`kiro_crew.dashboard.ws` directly, so progress leaves the engine only through the
injected event sink. `agent_exec`, `agent_pool`, and `store` are **optional
adapters**, not engine layers: `store.py` and `runner.py` import
`kiro_crew.config` / `kiro_crew.sel` / `kiro_crew.security` inside `try/except
ImportError` and degrade to a default or a no-op, so the engine stays importable
standalone.

`test/test_workflows_presence.py` additionally fails the build if any
`workflows/*.py` module is not imported by some `test/test_workflows_*.py`, so a
new module cannot land untested.

## The `ctx` DSL surface

`WorkflowContext` is a `@runtime_checkable` Protocol. The concrete implementation
is `runner._RunContext`, assembled per run. Every signature below is pinned
param-by-param (name, kind, default) and for async-ness by
`test_workflows_conformance.py::test_ctx_method_signature`.

### Inputs (the only clock and identity in scope)

| Attribute | Type | Semantics |
|-----------|------|-----------|
| `args` | `dict` | the run's arguments, from `workflow_run` / the HTTP body |
| `now` | `str` | a **fixed run-start stamp** supplied by the caller |
| `owner_dm` | `str` | the owner's Slack DM target, the intended argument to `send_slack` |
| `budget` | `Budget` | token accounting (below) |

`now` is fixed for the whole run on purpose. `time`, `random`, `uuid` and
`datetime` are unreachable inside a script (see Sandbox), so `ctx.now` is the only
clock in script scope, which is what keeps the deterministic call order (and thus
the resume prefix) stable. The event journal's `ts` is separate: the runner stamps
each event from a HOST wall clock (`host_now_iso`, real UTC) so the journal records
when each event actually happened, while `ctx.now` stays fixed for the script. The
runner also uses `time.monotonic()` in **host** code for the wall-clock guard and
the run duration; none of these host clocks are exposed to the script, so a
workflow gains no new time capability.

`_RunContext` also exposes `agent_results` (`call_index -> result` for calls
already settled in this run). It is in `validate.CORE_CTX_SURFACE`, so a script may
read it.

`owner_dm` doubles as the audit `runner` identity: the SEL records use `owner_dm or
run_id`.

> Open question: `WorkflowService` never passes `owner_dm`, so on the shipped
> gateway it is always the empty string and the audit `runner` field falls back to
> the `run_id`. A `ctx.send_slack(ctx.owner_dm, ...)` script would target an empty
> DM, though the surface check rejects such a script first because no host wires the
> `send_slack` port.

### Agent execution

```python
async def agent(
    self, prompt: str, *, label=None, phase=None, schema=None, model=None,
    agent=None, effort=None, cwd=None, session=None, nudge=None,
) -> AgentResult
```

Semantics, from `runner._RunContext.agent`:

- **Subagent by default.** Each call gets its own fresh, isolated session, so
  parallel calls share no conversational state. Pass `session=<key>` to run in a
  named shared session instead (a stateful chain).
- Order of operations per call: increment the per-run agent counter (raises
  `BudgetExceeded` past the cap), check the budget ceiling, emit `agent_started`,
  run the call, record the result, invoke the checkpoint sink, emit
  `agent_finished`, write one SEL audit record.
- `call_index` is the 0-based ordinal of the call within the run; `agent_id` is
  `f"a{call_index}"`.
- `phase` defaults to whatever the last `ctx.phase()` set.
- `label` defaults to `prompt[:40]`.
- With `schema=`, the call goes through `schema.run_with_schema` and resolves to a
  **validated** value, or `None` after bounded re-asks.
- **`BudgetExceeded` is the only exception `agent()` raises.** Every other failure
  is caught per call and resolves to `None`, with a bounded, redacted reason
  recorded in `agent_errors[call_index]` and on the `agent_finished` event's
  `error` field. A dead agent therefore does not fail the run
  (`test_workflows_runner.py::test_failed_agent_resolves_to_none_not_run_failure`).
- Three distinguishable `None` reasons are recorded, because "no payload" alone
  makes a post-mortem impossible: `"agent returned no result"`, `"no schema-valid
  result after bounded re-asks"`, and an exception rendered by
  `describe_agent_error` (type + message, redacted then truncated to
  `MAX_AGENT_ERROR_CHARS` = 500, no traceback).

`label`, `phase`, `schema`, `model`, `agent`, `effort`, `cwd`, `session` and
`nudge` are all passed to the injected `agent_fn` in one `opts` dict. The shipped
adapters read `session`, `agent`, `model` and `cwd`; `label` and `phase` are
consumed by the runner for the event stream; `schema` is consumed by the runner.

> Open question: `effort` and the per-call `nudge` dict are part of the frozen
> signature and reach `opts`, but neither shipped `agent_fn`
> (`agent_exec.build_agent_fn`, `agent_pool.build_pooled_agent_fn`) reads them, so
> today they have no effect. Either wire them or document them as reserved.

### Scheduling combinators

```python
async def parallel(self, thunks: list[Thunk]) -> list
async def pipeline(self, items: list, *stages: Callable) -> list
async def workflow(self, name: str, args: Optional[dict] = None) -> Any
```

`parallel` and `pipeline` delegate to `dsl.parallel` / `dsl.pipeline` with the
run's concurrency limit. `dsl.py` is a pure-asyncio leaf holding no agent or
gateway logic.

**`parallel(tasks)` is a barrier.** It runs every task concurrently, awaits them
all, and returns results in input order. A task may be a zero-arg thunk
(`lambda: ctx.agent(p)`) **or** an already-created awaitable (`ctx.agent(p)`);
both are accepted deliberately, because Python authors reach for the latter out of
`gather(*coros)` habit and a coroutine is not callable, which would otherwise turn
every task into `None`. A task that raises resolves to `None` and the call itself
never raises, so callers filter falsy entries. An empty list returns `[]`.

**`pipeline(items, *stages)` has no barrier between stages.** Each item flows
through all stages in its own chain, so item B can reach stage 2 while item A is
still in stage 1; wall clock is the slowest single chain, not the sum of the
slowest per stage. Stages are called `stage(prev, item, index)`, **arity-adapted**
(a 1-arg stage receives only `prev`); for stage 0, `prev` is the item itself. A
stage may be sync or async. A stage that raises drops that item to `None` and
skips its remaining stages. Results come back in input order; with no stages, the
items are returned as-is.

Both take an optional `limit` at the `dsl` level (a semaphore); the runner passes
its `concurrency`.

**`ctx.workflow(name, args)` is contract-only.** `_RunContext.workflow` raises
`NotImplementedError`. In practice a script never reaches it: `workflow` is not in
`CORE_CTX_SURFACE` and the runner's pre-exec surface check rejects the script with
`where="validate"` unless the host wires a `workflow` port, which no shipped host
does.

### Concurrency: two bounds, both needed

`concurrency` bounds agent calls **run-globally**: `_RunContext` holds one
semaphore acquired around each model call, so every `ctx.agent()` in the run queues
on the same slots. Each `parallel` / `pipeline` invocation *also* builds its own
semaphore. Both exist because the combinator limit preserves per-fan-out shape but
does not cover nested or sequentially overlapping combinators, nor agent calls made
outside any combinator
(`test_workflows_resilience.py::test_concurrency_is_bounded_across_separate_combinators`).
The run-global slot is held only across the model call, so no thunk holds a slot
while waiting for another to release one.

A third bound sits beneath both when the service has a task admission attached
(`WorkflowService(task_admission=...)` / `attach_task_admission`): every
`ctx.agent()` call is a `workflow_agent` row admitted through the shared runner
lane (`taskq.adapters.runner`, § Agent execution adapters below). The lane's
bound is the LIVE effective cap the adaptive controller moves through
`SubagentManager.set_effective_cap`; the lane reads it as its ceiling, and a
RAISE reaches its parked waiters as a `RunnerLane.pump()` from the manager
(`agent.adaptive_concurrency_mode=fixed` pins the bound instead). So a pressure
decision lowers workflow fan-out together with the subagent queue, and live
workers never exceed `min(max_workers, lane.effective)`.

Two facts about that occupancy, and they pull in opposite directions. The lane's
count is its OWN against the subagent gate's: the shared ceiling bounds runner
entries and queued sub-agents separately, not their total. But it is SHARED
between the two runner consumers — the gateway attaches one `RunnerAdmission` to
this service and to the TaskRunner, and one admission owns one lane, so a
`ctx.agent()` call and a TaskRunner step compete for the same slots (the `lane=`
argument is the row's label, not a second gate). Since an admitted call holds its
slot for the whole model turn, a call whose row DESCENDS from a row already
holding a slot would wait on a release only its own ancestor can make; the lane
refuses that wait (`RunnerLaneSelfBlocked`, reported as a failed call) instead of
parking on it for ever, and refuses nothing else —
[taskq.md](taskq.md) § A descendant is never parked behind its own ancestor.

### Progress

```python
def phase(self, title: str) -> None   # frozen signature; returns a CM at runtime
def log(self, message: str) -> None
```

Both are **synchronous** and must not be awaited. `ctx.phase(title)` sets the
current phase (later `agent()` calls group under it) and emits `phase_started`;
`ctx.log(message)` emits `log`. At runtime each returns a stateless context
manager so both `ctx.phase("read")` and `with ctx.phase("read"):` work, because
LLM authors write both. The context manager is purely cosmetic grouping: `__exit__`
does **not** end the phase or restore the previous one; a phase persists until the
next `ctx.phase()` call.

### Budget

`ctx.budget` is a host-owned, read-only binding to the run's `Budget` object,
not a token count or a timeout. The caller supplies `budget_total` when starting
a run; a script keeps its own limits in local variables and inspects the budget
through `total`, `spent()` and `remaining()`. A budget-only authoring lint rejects
obvious replacement/deletion through the entrypoint parameter and its closures.
A read-only property on `_RunContext` enforces immutability at runtime, including
aliases, globals and helpers outside the lint's scope. The lint is an early
authoring aid, not the enforcement boundary or the host's port-availability check.
This protects the binding only; the `Budget` accounting API is unchanged.

```python
class Budget(Protocol):
    total: Optional[int]      # None => no ceiling
    def spent(self) -> int
    def remaining(self) -> float
```

`total` is a **HARD ceiling, not advisory**. The concrete `context.Budget` adds
`would_exceed(cost=0)` and `charge(cost)`:

- `remaining()` is `math.inf` when `total is None`, else `max(0, total - spent)`.
- `charge(cost)` raises `BudgetExceeded` the moment cumulative spend **reaches**
  the ceiling (the comparison is `>=`, so a run cannot exceed the target), and
  clamps the stored counter to `total` so it never reads above it. The message
  reports the *attempted* spend.
- `runner._RunContext.agent` calls `budget.would_exceed()` **before** each call and
  raises `BudgetExceeded("budget exhausted before agent call")` if the ceiling is
  already reached, so `budget_total=0` fails the first call.

`BudgetExceeded` escaping the script is caught by the runner and becomes
`run_failed` with `where="ceiling"`. The same exception is raised by
`context.AgentCounter.increment()` once the per-run agent-call cap
(`DEFAULT_MAX_AGENTS_PER_RUN` = 1000) is reached, so a runaway fan-out lands on the
same terminal path.

> Open question: nothing in the shipped engine calls `Budget.charge()`. The
> ceiling is enforced (`would_exceed` before each call), but `spent()` stays 0 and
> `remaining()` stays at `total`, so a script's `ctx.budget.remaining()`-based
> early-stop never trips and no `budget_update` event is ever emitted. Wiring
> per-call token cost into `charge()` is the missing half.

### ports native to Kiro Crew

Four Protocols, each `Optional` on `ctx` and `None` when the host did not wire it:

| Port | Methods | Wraps |
|------|---------|-------|
| `CronPort` | `ensure(name, *, cron_expr, workflow, **kw)`, `add(name, *, every_secs=None, cron_expr=None, workflow, **kw)`, `remove(name)` | `apps.cron_sdk.CronSDK` |
| `MemoryPort` | `get(key, default=None)`, `set(key, value)` | `MemoryStore` |
| `LearnPort` | `add(rule, scope="workspace")` | `learn.LessonStore` |
| `KnowledgePort` | `async search(query) -> list` | `knowledge.KnowledgeStore` |

All four are `@runtime_checkable` so the runner can duck-type wrappers; the
conformance test pins that property and each method signature.

Four further primitives are **injected port functions** rather than Protocols, and
raise a clear `RuntimeError` naming the missing port when unwired:

```python
def nudge(self, *, idle_secs: int, message: str, max_cycles: int = 0) -> None
async def approve(self, prompt: str) -> bool
async def send_slack(self, target: str, text: str) -> None
async def send_message(self, channel: str, text: str) -> None
```

`ctx.nudge` is synchronous (it arms a loop and returns) and, like `phase`/`log`,
returns a no-op context manager at runtime.

**In the shipped gateway, `nudge` is the only wired port.** `WorkflowService`
builds `ports = {"nudge": _nudge}` and nothing else, so a script referencing
`ctx.cron`, `ctx.memory`, `ctx.learn`, `ctx.knowledge`, `ctx.approve`,
`ctx.send_slack` or `ctx.send_message` is **rejected before exec** by the
surface check (below) rather than crashing mid-run. The port plumbing and its
tests (`test_workflows_native.py`) exist so a host can wire them.

`ctx.nudge` never arms AutoNudge directly. The workflow's `session_key` is
caller-influenced (workflow endpoints derive it from the `X-Session-Key` header),
so the port delegates to a gateway-injected `nudge_authorizer` that runs the same
`authorize_and_add_nudge` chokepoint as `POST /api/autonudge`: slot existence,
Slack routability, Discord allowlist plus current-session match, the message-length
limit, and the SEL audit. Without that indirection a workflow could spoof another
session's key and mint a loop on it. Every outcome (armed / skipped / denied /
failed / undetermined) is reported back into the run's event stream as a `log`
event, so an unarmed nudge is never a silent no-op. Arm tasks are bucketed per run
and drained before the terminal event, bounded at 10s, so outcome logs land inside
the stream contract (terminal events last).

## Run event stream

The event vocabulary is frozen in `workflows/__init__.py`; `events.py` builds
well-formed events, assigns monotonic `seq`, and does the JSON round-trip.

Envelope (exactly these five keys, pinned by
`test_workflows_conformance.py::test_event_envelope_keys_exact`):

| Field | Type | Meaning |
|-------|------|---------|
| `run_id` | `str` | the run this event belongs to |
| `seq` | `int` | monotonic from 0 within the run, contiguous |
| `ts` | `str` | wall-clock stamp; the runner stamps each event from a HOST clock (real UTC), distinct from the fixed script-visible `ctx.now`. `EventStream` takes an optional injected `clock` (the runner injects `runner.host_now_iso`; tests monkeypatch that host function) and falls back to the caller-supplied stamp when it is unset. Event journals already on disk keep whatever stamp their writer supplied; a load does not and cannot reconstruct a real wall-clock time for events recorded before the host clock existed |
| `type` | `str` | one of `EVENT_TYPES` |
| `data` | `dict` | per-type fields, below |

`WorkflowEvent.to_json()` **raises `ValueError` on an unknown `type`**, so an
off-contract event fails loudly instead of shipping. `from_json` defaults a missing
`data` to `{}`.

`EVENT_TYPES` is an ordered tuple of 11 values; `events.REQUIRED_DATA_KEYS` covers
exactly that set (pinned by
`test_workflows_events.py::test_required_keys_cover_exactly_event_types`).
`validate_event` rejects an unknown type or a missing required key; **extra keys
are allowed** so a journal stays forward-compatible.

| `type` | required `data` keys | Notes |
|--------|----------------------|-------|
| `run_started` | `name`, `args`, `script_hash`, `budget_total` | first event of every run. `name` comes from `META["name"]` (empty on the author-in-run and validation-failure paths). `budget_total` may be `None` |
| `phase_started` | `title` | from `ctx.phase()`, and from the runner's own `"Authoring"` phase |
| `agent_started` | `agent_id`, `label`, `phase`, `call_index` | `agent_id` is `a<call_index>` |
| `agent_progress` | `agent_id`, `turns`, `last_tool`, `elapsed_s` | **builder exists, nothing emits it.** The frontend reads `last_tool` from it |
| `agent_finished` | `agent_id`, `result_summary`, `ok` | plus an optional `error` (bounded, redacted; empty when `ok`). `result_summary` is `str(result)[:120]`, empty when the result is `None`. `error` is deliberately **not** in `REQUIRED_DATA_KEYS` so journals from an older build still deserialize |
| `log` | `message` | from `ctx.log()`, from authoring progress, and from nudge outcomes |
| `budget_update` | `spent`, `remaining` | **builder exists, nothing emits it** (see the Budget open question) |
| `approval_requested` | `prompt`, `approval_id` | **builder exists, nothing emits it**; `ctx.approve` delegates straight to its port |
| `run_finished` | `result`, `duration_s` | terminal. `result` is the script's return value; `duration_s` is host `time.monotonic()` elapsed |
| `run_failed` | `error`, `where` | terminal. `where` is one of `author`, `validate`, `ceiling`, `exec` |
| `run_cancelled` | `reason` | terminal |

**Ordering invariants**, asserted by `test_workflows_runner.py` and
`test_workflows_authoring_eval.py` (G2): `run_started` is always first, exactly one
terminal event is always last, and `seq` is contiguous from 0. For the canonical
happy path the stream is `run_started, phase_started, log, agent_started,
agent_finished, run_finished`.

`where` maps to a cause:

| `where` | Cause |
|---------|-------|
| `author` | author-in-run raised, or produced no valid script |
| `validate` | static validation failed, or the script references a ctx primitive this host did not wire |
| `ceiling` | wall-clock timeout, `BudgetExceeded`, or the per-run agent-call cap |
| `exec` | the script itself raised |

### Persistence of the stream

`events.serialize_events` / `deserialize_events` go through `json` only, never
`pickle` or `marshal`; `deserialize_events` rejects a non-array payload and
re-validates every event. This is a hard rule for the whole subsystem: run records
are JSON on disk and JSON on the wire.

## Sandbox and static validation

A workflow script is LLM-authored code, so `validate.validate(source)` runs before
any `exec`. It returns a `ValidationResult(ok, errors, meta)` and **never raises**
on a bad script (only if `source` is not a string). The runner re-validates
immediately before `exec` as defense in depth, so a future refactor that skips the
caller's validation cannot reach `exec` unchecked.

Static rejections:

- **No `import` / `from ... import`,** at all. Imports of `time`, `random`, `uuid`,
  `datetime`, `secrets`, `os` get a sharper message (determinism and egress), but
  every import is rejected regardless.
- **`FORBIDDEN_NAMES`:** `eval`, `exec`, `compile`, `open`, `input`, `__import__`,
  `__builtins__`, `globals`, `locals`, `vars`, `getattr`, `setattr`, `delattr`,
  `breakpoint`, `memoryview`, `classmethod`, `staticmethod`, `super`, `type`.
- **No dunder attribute or name access** (`().__class__`, `__builtins__`).
- **Shape:** a module-level `META` assigned a **pure literal** dict
  (`ast.literal_eval`; both `META = {...}` and `META: dict = {...}` are accepted),
  and an `async def workflow(ctx)` entrypoint whose first parameter is named `ctx`
  (positional-only `def workflow(ctx, /)` is fine). A plain `def workflow` is
  rejected with a specific error.
- **Size:** `MAX_SCRIPT_BYTES` = 262144 bytes of UTF-8.
- **Undefined names** (`_check_undefined_names`, via `symtable`): any referenced
  global that is not a safe builtin, not `ctx`, and not bound at module level is
  reported, so a script using `json` or an unlisted exception type is caught at
  authoring time instead of dying mid-run with `NameError`. Conservative by design:
  only names the symbol table marks as referenced-global-and-never-assigned are
  flagged, and walrus / `global` bindings are treated as bound.
- **DSL-contract misuse** (`_DslContractVisitor`). These are structurally legal
  Python, so the security pass and the name pass both wave them through; only a
  contract-aware pass catches them. A type checker would too, but mypy is not
  importable in the sandboxed runtime, so this deterministic AST lint is the
  enforceable gate, and it runs at authoring time so a bad script regenerates
  instead of failing mid-run:
  - `await ctx.phase(...)` / `await ctx.log(...)` / `await ctx.nudge(...)`, because
    those are synchronous.
  - `with ctx.<m>(...)` for any `m` outside `{phase, log, nudge}`, and `async with
    ctx.<anything>(...)` at all.
  - inline dereference of a nullable awaited result:
    `(await ctx.agent(...)).get(...)` or `[...]` or `.attr`, for `agent`,
    `parallel`, `pipeline`, `workflow` and `approve`. Binding to a variable first
    and guarding is the correct pattern and is intentionally not flagged.
  - assignment to or deletion of the workflow context's `budget` binding,
    including annotated/augmented assignments, unpacking and loop targets. The
    budget-only authoring aid is independent of the host-surface check and
    supports positional-only workflow entrypoints. Nested helpers' own parameters
    and locals (including assignment, exception and loop targets) are unrelated
    state; closures and `nonlocal ctx` still refer to the nearest enclosing
    binding. Global/module references are ambiguous and rely on the runtime
    read-only property, like other aliases. No runtime binding, control-flow or
    alias inference is attempted; legitimate module-owned `ctx.budget` writes
    remain legal. Budget reads and mutations of script
    data such as `ctx.args["stage"]` remain allowed. The diagnostic directs the
    author to the caller's `budget_total` or a script-local variable; the existing
    bounded authoring loop can then request a corrected script before execution.

The runtime half of the sandbox is `context.build_safe_globals(ctx)`: the script is
`exec`'d with a `__builtins__` built from exactly `validate.SAFE_BUILTINS` plus
`ctx`. `__import__` is deliberately absent, so an `import` statement fails at run
time even if one slipped past the AST pass, and there is no filesystem or socket
capability in scope. `SAFE_BUILTINS` is a set of value and collection builtins
(`len`, `range`, `sorted`, `dict`, `isinstance`, and the like) unioned with
`SAFE_EXCEPTIONS`, a set of pure exception classes so ordinary `try/except
Exception` works. Exception classes grant no capability, which is why they are
safe to expose. Those two frozensets are the single source of truth shared by the
validator and the exec namespace, so there is deliberately no second copy of the
allowlist to drift.

`test/workflows/malicious/*.py` is an adversarial escape corpus; every file in it
must be statically rejected
(`test_workflows_malicious.py::test_every_malicious_script_is_rejected`). New
escape ideas are added by dropping a file in that directory. If a case in that
corpus or in `test_workflows_invariants.py` flips from red to green because a check
was loosened, that is a sandbox regression, not a fix.

### Host-aware surface check

`validate` cannot know which ports a given host wires, so the runner calls
`validate.check_ctx_surface(source, available)` at the exec boundary with
`CORE_CTX_SURFACE | {wired port names}`. `CORE_CTX_SURFACE` is the always-available
set: `agent`, `parallel`, `pipeline`, `phase`, `log`, `budget`, `args`, `now`,
`owner_dm`, `agent_results`. A reference to anything else fails the run with
`where="validate"` and an error listing the available surface, instead of crashing
mid-run with `RuntimeError("... no ... port wired")`.

The host check conservatively walks the entrypoint body and nested functions,
skipping a helper whose own parameter shares the context name (for example,
`def read(ctx): return ctx.get("k")`, called with a dict). It does not infer
ownership from module assignments, `global` declarations or helper-local stores:
these cannot suppress an unwired-port diagnostic, even when a binding is in an
unexecuted branch. An available attribute such as `budget` remains allowed here;
write protection belongs to the separate budget lint and runtime property.
This preserves the host check's existing limits: helper-local methods outside
the available surface can be conservatively rejected; helpers receiving the real
context through their own parameters are under-enforced. The host walk uses the
first ordinary positional parameter, so positional-only entrypoints do not gain
host-surface coverage from the budget lint's positional-only support.

## Structured output (`schema=`)

`schema.py` is a dependency-free validator for the JSON-Schema **subset** the DSL
actually uses. Kiro Crew's provider layer has no native schema enforcement and the
runtime ships neither `jsonschema` nor a JSON-Schema-consuming pydantic path.

Supported: `type` (`object`, `array`, `string`, `integer`, `number`, `boolean`,
`null`, or a union list of those), `properties`, `required`, `items`, `enum`.
Unknown keywords are ignored, so a richer schema still validates on the parts this
understands. `bool` is explicitly **not** an `integer` or `number`.

`parse_json(text)` is tolerant of the two shapes a model actually returns: a
fenced ` ```json ` block, and JSON wrapped in prose. Prose recovery delegates to
the shared `llm_helpers._extract_json_of_type` scanner (stdlib `raw_decode` over
successive `{` / `[` offsets), so a stray brace or bracket in the prose no longer
corrupts recovery, and a prose-wrapped array still yields the outer array, not an
inner object. `coerce_and_validate` derives a prefer predicate from the schema's
container `type`, so an object schema selects the object even when stray prose
parses as an array first (and vice versa); two *different* candidates of the
preferred shape refuse the guess and count as a parse failure, feeding the retry
loop. It never `eval`s.

`run_with_schema(produce, prompt, schema, retries=DEFAULT_SCHEMA_RETRIES)` drives
the loop: the prompt is augmented with the serialized schema and a JSON-only
instruction, and on malformed or invalid output the model is re-asked up to
`retries` more times (default 2, so 3 attempts total) with the validation errors
appended so it can self-correct.

**A schema violation is not a run failure.** After the bounded retries,
`run_with_schema` returns `None`, `ctx.agent()` returns `None`, and
`agent_errors[call_index]` records `"no schema-valid result after bounded
re-asks"` while `agent_finished.ok` is `False`. Distinguishing that from an
exception matters: it means the model answered but never matched the schema, which
is a prompt or schema problem, not an infrastructure one. The script is expected to
None-guard, and `validate` rejects the inline unguarded dereference.

## Ceilings

| Ceiling | Constant | Value | Behavior at the limit |
|---------|----------|-------|----------------------|
| Wall clock per run | `runner.DEFAULT_RUN_TIMEOUT_SECS` | 3600s | `run_failed`, `where="ceiling"`, `error="timeout"` |
| Wall-clock bounds | `MIN_RUN_TIMEOUT_SECS` / `MAX_RUN_TIMEOUT_SECS` | 60s / 21600s (6h) | a caller value is clamped into the range |
| Agent calls per run | `context.DEFAULT_MAX_AGENTS_PER_RUN` | 1000 | `BudgetExceeded` -> `run_failed`, `where="ceiling"` |
| Tool calls per agent step | `agent_exec._MAX_TURNS_PER_STEP` | 200 | `stream_and_collect` stops the step; prevents an infinite tool loop from prompt injection |
| Token budget | `budget_total` per run | caller-set, `None` = unbounded | `BudgetExceeded` -> `run_failed`, `where="ceiling"` |
| Script size | `validate.MAX_SCRIPT_BYTES` | 262144 | validation error |
| Schema re-asks | `schema.DEFAULT_SCHEMA_RETRIES` | 2 | result is `None` |
| Tracked runs in memory | `registry.DEFAULT_MAX_RUNS` | 200 | oldest **terminal** run evicted; a running run is never evicted |
| Persisted agent-error text | `runner.MAX_AGENT_ERROR_CHARS` | 500 | truncated after redaction |

`clamp_run_timeout(value, default=...)` is the one door to the wall-clock ceiling.
`None`, non-numeric, and non-positive input fall back to the default, so a bad or
zero value can never remove the ceiling, and any accepted value is floored at 60s
(a run needs time to author itself) and capped at 6h. The service default comes
from config `agent.workflow_run_timeout_secs`; a per-run `timeout_secs` overrides
it for that run only, through the same clamp.

The wall-clock guard uses `asyncio.wait({task}, timeout=)`, **not**
`asyncio.wait_for`: with `wait_for`, a timeout that races task completion can leak
the inner `CancelledError` to the caller. With `wait` the loop never cancels for
us, so the runner owns the cancellation and always converts a timeout into a clean
`run_failed` (`test_workflows_runner.py::test_timeout_never_leaks_cancellederror`).

**The ceiling is a backstop, not a data-loss event.** Every terminal path (ceiling,
cancel, budget, script crash, success) returns the `agent_results` and
`agent_errors` collected so far, and each call is checkpointed onto the run record
as it settles rather than only after the run ends
(`test_workflows_resilience.py`). Without that, a run killed at the ceiling would
write `agent_results: {}` and discard every payload it had already paid for.

On a **caller cancellation** the runner cancels the in-flight script and drains
it before emitting `run_cancelled`, as on the timeout path. Cooperative cleanup
and checkpoints settle before the terminal event, and cancellation exceptions are
consumed. After the initial cancellation, the runner shields and repeatedly drains
ordinary script cleanup, the pre-terminal hook, and background completion cleanup;
further cancel requests cannot cancel their awaited work. Cleanup exceptions are
retrieved without replacing an already-selected timeout, failure or cancellation.
A successful script return alone does not commit success: the pre-terminal hook
reports any caller cancellation it observes after draining, and the runner emits
one `run_cancelled` instead of `run_finished`, without rerunning the hook. Publishing
a terminal event commits the outcome. During subsequent background completion
cleanup, registry cancellation returns false even while the final registry status
is pending; direct shutdown cancellation still drains cleanup without rewriting
that event. Both paths depend on adapters cooperating with cancellation; they do
not forcibly terminate Python code that ignores cancellation. A terminal status
must not be published merely because a cleanup deadline elapsed while owned work
is still executing.

## Run registry, persistence, and resume

### `registry.py`

`RunRegistry` is an in-memory, **loop-affine** bounded-LRU store of `RunHandle`
records: all mutation happens on the gateway event loop, so no locks are needed,
and snapshots handed to callers are plain JSON-serializable dicts, never the live
objects (in particular never the `asyncio.Task`).

`RunHandle` holds `run_id`, `name`, `status`, the growing `events` list, `result`,
`error`, `author`, `session_key`, `source`, `args`, `agent_results`,
`agent_errors`, exact saved-definition provenance, optional `derived_from`
ancestry, and the driving `task`. Statuses: `running`, `finished`, `failed`,
`cancelled`.

Two distinct serializations:

- `snapshot(include_events=, include_result=)` is the **UI view**. The compact
  form (`include_events=False, include_result=False`, what `list()` builds) adds
  derived live progress (`phase` from the last `phase_started`, `last_log` from
  the last `log`) plus `partial_result_count` / `agent_error_count`, and omits
  the `result` payload — a finished run's result can be hundreds of KB, so it
  rides only on the detail view, exactly like `events` and `source`. The full
  form adds `result`, `events`, `source`, `partial_results`, `agent_results` and
  `agent_errors`.
  The `on_done` completion snapshot keeps `include_result=True`, so
  result-to-chat injection is unaffected. Partials are keyed on
  **status**, not on `result is None`: a run can finish and legitimately return
  `None`, and a running run has no result yet, so neither lost anything and
  reporting partials for them would mislead the reader and resend every payload on
  every poll. A **finished** run additionally exposes its settled per-call outputs
  — `agent_result_count` in the compact form and `agent_results` (the payloads) in
  the full form. This is additive and status-keyed: `partial_results` stays the
  failed/cancelled channel so a run never reports both, an active run omits both
  (it is still accumulating), and the exposure is a factual record of the calls
  that settled, not a claim the run produced a validated business artifact. The
  `workflow_result` MCP projection includes these outputs through the same
  recursive redaction as the aggregate result and errors. The completion message
  names the recorded-call count and this field, and explicitly separates a
  returned workflow function from verified required artifacts.
- `to_store_json()` / `from_store_json()` is the **durable** round-trip of the
  complete run. `from_store_json` demotes a stored `running` run to `failed` with
  `"interrupted: gateway restarted while running"`, because it can never resume in
  a new process and would otherwise wedge the registry as a zombie that eviction
  refuses to reclaim.

Before each model turn, authoring, pooled workers and named sessions publish the
ordinary strict transport identity for their acquired key. The run itself owns
one frozen `execution_context` in `RunHandle` and its ordinary JSON checkpoint.
Service admission captures it once off-loop before admission and passes it into
the handle before initial registration. A supplied carrier bypasses parent reads;
later awaits and parent closure cannot change its routing. Workers receive that
same record; no protected workflow binding or hidden private payload copy exists. Status,
result and mutations retain ordinary caller, owner/app and governance checks.
Scope admission snapshots stricter live gateway restrictions and inherited modes
before capture awaits, then combines them with the captured mode. Its live policy
port does not read persisted parent records; an absent parent cannot invalidate an
owned carrier, and invalid retention inputs still refuse admission.

The carrier freezes the member ID, store ID, namespace, template, app and privacy
mode. Reruns use the prior handle's carrier even if the parent closed, and combine
its mode with any stronger requesting mode. Incognito and Temporary runs stay in
memory and never write source/events/results through the checkpoint store.
Their agent calls use storeless task admission over the same live lane and
resource-pressure checks. Call parameters, failure details and dependency waits
stay in memory; cancellation releases the shared slot without a durable task row.
Incognito may read memory; Temporary suppresses reads. Both refuse learned-memory
writes. Missing or malformed member identity is an error, never Global fallback.
Admission still checks gateway closure before and after awaited registration and
never launches an unreturned run.

`mark_terminal` is idempotent: only the first terminal transition counts. Its async
counterpart is used by live workflows and host drivers; it drains queued checkpoints,
flushes the selected outcome, then fires `on_done` (result-to-chat injection). The
in-memory terminal status is visible before that durable flush and callback complete,
so tests asserting delivery wait for a callback-owned signal with a bounded timeout
rather than sleeping after observing terminal status.
`record_event` fans out to `on_event` (live WS push). Dynamic workflows keep that
callback synchronous and queue an owned async checkpoint every fifth event; authored
source publication uses the same queue. The synchronous `ctx.log`/`ctx.phase` contract
and immediate in-memory agent-result settlement are unchanged. `record_agent_result`
does not force a separate write: durability rides the five-event cadence plus the
terminal flush. A hard gateway kill can lose payloads no flush has covered yet.
A raising `on_event` / `on_done` subscriber is swallowed.

Dynamic registration and host registration both await off-loop eviction and initial
persistence before returning the identity. Cancellation drains admission before
removing its partial run; no driver is launched for an unreturned identity. Source,
intent and subtree-rerun entrypoints recheck gateway admission after this new await;
a closed gate deletes the unlaunched run and returns the existing admission error.
All permitted persistent writes use the existing per-run
snapshot/generation/lock path. Tests simulating an orderly restart await the
background driver's terminal flush before constructing the replacement service;
a terminal status in RAM alone is not a durable-completion barrier. A delayed-write
regression pins this distinction without weakening the restored-source checks.
Repeated cancellation drains owned writes and deletes
before propagating. Terminal persistence cannot reinterpret the outcome already
selected by the runner, and completion injection waits for that flush attempt.
A failed save is not silent: directory preparation, serialization and temporary
write/replace failures propagate from the store (after cleanup is attempted) to
the registry. Their diagnostics contain only exception types, not private paths
or payloads. A fixed, sanitized storage message is included in
the run snapshot's `error` alongside any execution error, in completion messages
(including a finished run), and in the run detail's `ErrorNotice`. It tells the
operator to copy needed results before restart and check storage access/space.
For non-failed runs with the backend's `error_code=workflow_checkpoint_failed`,
the shared run tree renders a localized checkpoint summary and a closed-by-default
diagnostic disclosure, not a clipped English backend sentence. The code is emitted
only for a pure checkpoint error, never inferred from status or English prose;
execution/cancellation errors and unknown legacy codes retain their original text.
The full sanitized, credential-redacted diagnostic remains readable on
expansion and accompanies the notice's agent hand-off as a system report, not
an invented HTTP failure. Execution failures retain their separate presentation.
Execution status/result remain distinct from this live durability health. The
next successful checkpoint clears only the storage message; no separate retry
timer or durable-ack guarantee is introduced. Synchronous registry APIs remain
for standalone compatibility, not live workflow callbacks.
Startup inventory is not best effort: both service construction paths propagate
store-open/load/directory-enumeration failures rather than publish an empty
registry and reset the legacy ID floor to zero. Missing directories on first
boot are empty only when their nearest existing ancestor is a directory. This
also checks Windows path-not-found errors beneath a plain file, rather than
mistaking that broken inventory for a first boot. An unreadable or non-directory
inventory is an error.

The trusted host lifecycle follows the same off-loop persistence rule. Its
service methods mutate loop-affine handles and event streams on the event loop, then
await async registry checkpoints for registration, binding/rebinding, source updates,
status transitions, throttled events, terminal flushes, and deletion. Each checkpoint
snapshots the handle on the event loop, then moves only the durable write through
`asyncio.to_thread`. TaskRunner plans can reach the 256 KiB request ceiling, so no
host lifecycle checkpoint may stall the gateway event loop or move live registry
state across threads. Cancellation drains an in-flight registration write before
asynchronously deleting the partial run, so a late atomic replace cannot resurrect an
identity that was never returned to its host driver.

The read handlers extend the same rule to the response path: the run list, run
detail, and definition list/get endpoints serialize the payload once
synchronously on the event loop (an atomic snapshot — no await, so no
loop-driven mutation can interleave), then a worker thread rebuilds its own
structure from that immutable string and performs redaction + the final JSON
serialization. The thread only ever touches objects it created itself, so it
can never observe or race live registry state.

### `store.py`

`WorkflowRunStore` persists **one self-contained JSON file per run** at
`<workflows dir>/runs/<run_id>.json`. The directory resolves from a `workflows.dir`
config key when present, else `<config_dir>/workflows` (so `~/.kiro/crew/workflows`
by default, honoring `KIROCREW_HOME`).

> Open question: `default_workflows_dir()` reads `cfg.workflows.dir` defensively
> through `getattr`, but the shipped config dataclass has no `workflows` section,
> so the config path is currently unreachable and every install lands on
> `<config_dir>/workflows`.

Properties that matter:

- **Atomic writes:** temp file plus `os.replace`, then `chmod 0o600`, so a crash
  mid-write cannot corrupt a run file.
- **Redaction before disk:** every string in the record passes through
  `redact_exfiltration_urls` then `redact_credentials`, recursively. Defense in
  depth; the HTTP and chat surfaces redact again on the way out.
- **Injective paths:** a `run_id` is sanitized to alphanumerics plus `_`/`-` so a
  malformed id cannot traverse out of the runs dir. Because sanitizing is lossy,
  when it changes the id a 12-hex-char sha256 prefix of the original is appended,
  so `wf/1` and `wf1` cannot collapse onto one file. Well-formed ids
  (`wf_NNNNNN`) are unchanged.
- **One owner record:** every persistent run writes its complete record and
  canonical execution context to this ordinary run path. Restricted modes are
  suppressed defensively by both registry and store. No second inventory or
  member-specific payload root exists.
- **Explicit checkpoint failures:** serialization/write/replace failures propagate
  as fixed sanitized errors while live execution results remain available.
  Recovery retains ordinary descriptor/path/owner checks, rejects malformed
  records and returns records oldest-first. Inventory access failures propagate;
  corrupt individual records cannot manufacture a replacement member identity.

`RunRegistry.load_persisted()` rehydrates on startup, fills only ids not already
in memory, and re-runs eviction so a store with more records than `max_runs`
cannot leave the registry over its bound. A record stored as `running` was
demoted to `failed` by `from_store_json` (it can never resume in a new process);
`load_persisted` then persists that corrected state back to the store,
idempotently — only the `running`→`failed` case writes, an already-terminal
record is left untouched — so the durable record matches memory and a later
restart does not re-demote the same zombie every time. The writeback goes through
`to_store_json`, so agent payloads, source, provenance and args are retained.

After the socket binds and instance credentials are published, both server entrypoints
schedule one owned initialization task for `WorkflowService.create()`; neither
awaits user-data-scaled rehydration on the boot-to-ready path. Until initialization
settles, `workflow_service` remains unpublished and workflow routes return their
existing 503 unavailable response. Before its first startup await, the dashboard
calls `TaskRunner.defer_workflow_attachment()` on the shared gateway instance.
Plan, direct run, background start, execute-plan and retry admission then refuse
with an initializing/retry message before creating run state. This covers channel
callers retaining `orch.task_runner` as well as dashboard admission paths.
`attach_workflow_service(service)` releases admission atomically with attachment;
`attach_workflow_service(None)` explicitly releases standalone fallback. Gateway
failure cleanup detaches and immediately defers again without yielding, preserving
fail-closed workflow admission with `workflow_initialization_failed` and a restart
message. Pending recovery alone asks callers to retry. If storage blocks
indefinitely, mutation requests deliberately keep returning 503 with
`code: workflow_initializing`; reads and cancellation remain available. Treating
slowness as standalone mode would admit permanently unlinked fresh runs and permit
linked task records to diverge while the original restore worker still exists.
An explicitly selected standalone host is different from a failed gateway;
only standalone hosts use the established best-effort publication fallback. Ordinary standalone
and headless constructors do not defer admission. The dashboard retains its runner
pointer throughout initialization, so existing-task status and cancellation remain
available. Deletion and plan/step edits require the same readiness boundary before
any mutation: otherwise their workflow propagation would be skipped during restore.
All task admission and linked-mutation handlers translate `WorkflowInitializing`
from the shared check to an initializing 503, not a generic runtime error.
Chat-to-plan checks before creating its placeholder; status and cancel do not
require workflow readiness.
The task attaches both directions and releases admission without an intervening
await. Initialization failure is logged, keeps workflows unavailable, and keeps
TaskRunner mutations closed until a gateway restart. A non-blocking aiohttp `on_shutdown`
hook fences publication before cleanup begins, including while tunnel teardown is
stalled. Tunnel teardown remains the first `on_cleanup` hook; only after it finishes
(or reaches its existing bound) does workflow cleanup cancel and drain initialization,
even under repeated cancellation. A factory returning during tunnel teardown or
after cancellation cannot publish.

`WorkflowService.create()` constructs the unpublished service off-loop, including
run-store config lookup and definition-library path resolution. The constructor
creates no asyncio tasks or locks; `live.bind` registers its weak setter under the
watcher's thread lock without invoking it. Cancellation drains the owned constructor
before propagating, even if construction subsequently fails. Registry hydration
then runs on the owning loop. `WorkflowRunStore.load_all()` reads every run JSON
before registry eviction: `DEFAULT_MAX_RUNS=200` bounds retained memory, not disk
scan count or payload size, and is not an upper bound on startup I/O.

The `load_persisted_async()` path reads records, writes
restart corrections, and deletes evicted records off-loop; handle hydration and
mutation remain on the owning loop. This is startup-only on an unpublished
registry, never concurrent with live runs or host reopen. Cancellation (including
repeated cancellation) drains the owned load before propagating, leaving no late
writer to race a subsequent initialization. The synchronous constructor and
`load_persisted()` remain available for standalone callers. Both paths retain the
store's best-effort failure semantics: awaiting I/O does not certify a successful
write when the store itself reports failure only through debug logging.

### Reusable definition library

Every invocation still persists its authored script as part of the run snapshot
above. Promotion into the reusable library is a separate, explicit user action.
`WorkflowDefinitionLibrary` stores one JSON record per saved workflow at
`<KIROCREW_HOME>/workflow_library/<workflow_id>.json`, which is global to the
Kiro Crew data home and therefore shared by Kiro and adapted harnesses such as
KAS. Unlike run snapshots, definitions do not follow the configurable
`workflows.dir`: `workflow_library` is a dedicated `_CREW_SECRET_LEAVES`
directory, so the shared sensitive-path gate blocks agent file tools and shell
commands from planting or rewriting executable definitions. Dashboard and
workflow-service operations open the directory directly after the human action.
The create and revision endpoints require a positively authenticated dashboard
user, not an app token or internal agent call. An app token is also refused before
a saved run accepts its caller-supplied session header, so it cannot spoof result
delivery into another live session. Every allow or deny decision at these
authorization gates is SEL-audited.

A definition has a stable `wfd_*` id, unique slash-command `slug`, current source,
SHA-256 content hash, timestamps, an integer `revision`, immutable source entries
in `revisions`, an immutable `format` (`python` or `task-plan`), and optional
`derived_from: {workflow_id, revision}` lineage. Schema-v1 records omit `format`
and load as `python`; newly written records use schema v2. Python and TaskRunner
YAML sources are validated by their owning parser before persistence.
Every explicit save creates a separate definition identity, including when its
source is byte-identical to a parent it adapted. Updates require `expected_revision`
and append a revision; a stale writer receives a conflict instead of overwriting
newer content. The dashboard snapshots the submitted editor state: a successful
save advances its revision but preserves any further edits made while the request
was pending. An omitted or blank slug on update preserves the existing slash
command. Writes are atomic. Metadata is redacted before slug normalization
and disk persistence, so normalization cannot hide a credential from the scanner;
if credential or exfiltration redaction would change executable source, the save is rejected so
the library never persists a corrupted script. Async gateway, authoring, and saved-run
paths offload library disk I/O to worker threads, while the service serializes library
operations so slug allocation and revision checks remain atomic in-process.

Only explicitly saved definitions participate in local authoring matches.
`search(intent)` uses deterministic local lexical ranking. `author(intent)` adds
the bounded top matches to the authoring prompt as examples, never edits them,
and accepts lineage only when the authored `META["adapted_from"]` exactly names
the current revision of one of the candidates included in that prompt. Historical
revisions embedded in a candidate record are not offered authoring references.
The authored result remains an unsaved draft until `save_definition` is called.
An explicit id or slug reference takes the opposite path: `start_definition`
executes the exact current saved source. Python definitions place free-form
slash-command input in `ctx.args["input"]`. `task-plan` definitions delegate
their exact YAML to TaskRunner without LLM decomposition and retain the saved
definition id, slug, and revision on both the project and shared run.

The chat run card offers explicit promotion after an ad-hoc Python run reaches
`finished`. Agent Capabilities > Workflows has **Workflow library** and **Runs**
views; the Runs view lists both dynamic Python and TaskRunner-backed runs. It
also offers promotion for a paused or finished TaskRunner plan whose run
declares the `save` capability. It loads the response-redacted run snapshot for
its compact read-only, format-highlighted preview, but submits only the run id
plus user-entered name, slash-command slug, and description to the promotion
route. The service reads the original source from the server-side run handle and
passes those exact bytes to `save_definition`; display redaction can therefore
never become executable source.
The durable store records whether source bytes survived redaction unchanged. Exact
restored source remains promotable; redaction-changed and legacy source lacks exact
provenance and cannot use this route. An unedited rerun preserves that provenance;
a genuinely edited rerun is exact to the submitted edit.
Running, failed, and cancelled runs are not promotion candidates. The definition
id on an exact saved invocation also makes it ineligible: it is already reusable
and must not mint an accidental duplicate definition. The definition
editor uses the same line-numbered, horizontally scrolling source surface in
editable mode. Python is highlighted as Python and TaskRunner plans as YAML. Both
source views are presentation-only:
neither normalizes nor reformats source bytes. On success, the card shows
`/workflow <slug>` and links to Agent Capabilities > Workflows.

Session promotion derives lineage from the validated original source's
`META["adapted_from"]` and accepts it only when the exact id and revision exist in
the library; historical revisions remain valid after the parent advances. The
general definition-create route does not infer lineage from source: a management
surface must submit an explicit `derived_from` object, which receives the same
existence check.

### Resume and restart-subtree

Resume is prefix replay, not checkpointing of arbitrary state. `runner.run` takes
`replay_results` (a `call_index -> result` map from a prior run) and
`replay_before` (an index): a call with `call_index < replay_before` that has a
cached entry returns that entry instead of calling the model; calls at or after the
index re-execute live. Determinism is what makes this sound: no `time`/`random` in
scope plus a stable `call_index` means the same script and args produce the same
call order. A replayed call whose prior result was `None` records
`"replayed a call that had already failed in the prior run"`.

`WorkflowService.rerun_subtree(run_id, from_index, source=None)` drives it from the
stored handle. If `source` is supplied and differs from the stored script, the
rerun is treated as **edited**: the edited source is validated first (rejected with
errors if invalid) and the replay cache is **not** reused (`replay_before=0`,
`replay_results={}`), because editing the script can shift call indices and a
mismatched index would replay the wrong result into the wrong step. An unedited
rerun of a saved definition retains its definition id, slug, and revision so it
remains an exact saved invocation and cannot be promoted into a duplicate. An
edited rerun clears that saved-definition identity because its submitted source
is no longer the exact saved revision.

TaskRunner applies the same rule to saved task plans. A user edit or automatic
replan clears the run's exact definition id, slug, and revision before publishing
the changed YAML. The run retains the former id and revision only as
`derived_from` ancestry, so promoting the adapted plan creates a distinct saved
definition with correct lineage instead of falsely attributing changed source to
the original revision.

## Gateway wiring

`service.WorkflowService` is the single façade the gateway constructs once at
startup (`DashboardState.workflow_service`); handlers reach it via request app
state, like `state.subagents` / `state.sessions`. It owns one `RunRegistry` (with a
`WorkflowRunStore` unless `persist=False`) and builds a fresh `WorkflowRunner` per
run.

After the dashboard (or the headless API server) is up, the gateway's
`_wire_runner_admission` builds ONE `RunnerAdmission`
(`taskq.adapters.runner.runner_admission_for` over the subagent manager's
store and effective cap) and attaches it to both the TaskRunner and this
service; the manager's `DependencyCoordinator` gets the admission's `on_wake`
(and its `on_fail`, as a wake) through `coordinator.subscribe(...)`, so a 429
seen by a workflow agent call and one seen by a sub-agent share one retry
schedule.

That first pass can run before the coordinator exists: a manager built on the
loop opens its store on a worker, and there is no coordinator until there are
rows. It is deliberately not deferred — both consumers need the admission (and
its typed refusal) as soon as the socket is bound. So `run()` awaits a second,
idempotent pass, `_runner_admission_store_ready`, right after
`wait_taskq_ready()`: it binds the coordinator, subscribes, re-reads the waiting
rows and runs the adoption sweep the store-less pass skipped
([taskq.md](taskq.md) § Runner adapters). Binding it is not optional — the
reaper pump calls the admission's own `tick()` only while there is NO store, so
a wait parked in that window would otherwise have no wake path at all. The
fallback `tick()` is the steady state only for a durable queue that is genuinely
off. Shutdown detaches it (`attach_task_admission(None)`).

Entry points: `author`, `start`, `start_from_intent`, `status`, `result`,
`list_runs`, `cancel`, `rerun_subtree`, `list_definitions`, `get_definition`,
`save_definition`, `update_definition`, and `start_definition`, plus the trusted
host lifecycle (`begin_host_run`, `bind_task`, `phase`, `log`, `step`, `pause`,
`rebind`, `finish`, `fail`, `cancel_host_run`, `delete_run`) and the
`timeout_secs` property. Every trusted host lifecycle mutation is async when it can
produce a durable checkpoint, so host drivers await the off-loop persistence path.

Dynamic workflows freeze their canonical execution context before scheduling.
The ordinary run record owns identity and payload together; the in-memory
`RunHandle` owns restricted runs. Author attempts and every worker receive the
frozen carrier before SessionManager allocation and prompt construction. Named
sessions are local run labels, not references that can adopt arbitrary chat
memory. Missing or wrong-store data produces an explicit memory error.

The author retains REJECT_ALL, and ordinary caller, app/owner, tool and governance
ceilings stay in force. Member memory is not an additional authorization domain.
Reruns inherit their original handle rather than today's member alias, template
or parent. Completion delivery checks the original run record and ordinary
recipient permissions.

Host-driven runs carry `driver`, `source_format`, `task_id`, `capabilities`, and
saved-definition provenance in every compact and full snapshot. `paused` is an
active, durable workflow status. A restored paused host run, or a host run
demoted to failed after interruption, rebuilds its event stream at the next
sequence number and reopens the same identity when TaskRunner resumes it. Reopening a
terminal host run first removes its trailing terminal event, so the retried attempt
still has one terminal event at the end of a contiguous journal. Host drivers keep
their own completion/reporting path by setting `completion_injection=false`.
Off-loop checkpoints are serialized per run. When parallel host steps queue
multiple snapshots behind an active write, superseded intermediate snapshots are
discarded and the newest snapshot is written last; deletion uses the same queue
so an in-flight checkpoint cannot resurrect a removed run.

Run ids are `wf_NNNNNN` from a per-process monotonic counter, deliberately with no
time or random component so they stay resume-stable. On startup, after rehydrating
persisted runs, the counter continues past the highest persisted sequence so new
ids cannot collide with restored ones.

### Authoring

`author(intent)` turns a natural-language intent into a validated script using the
same in-session model plumbing as the rest of Kiro Crew. Session creation has its
own narrow startup retry: up to three total attempts, each with a fresh isolated
key and deterministic bounded backoff, only for `AcpRequestTimeout`,
`AcpRuntimeDead`, or an `AcpError` accepted by the shared transient classifier.
Each failed attempt is destroyed before the next. Authentication, configuration,
permission, arbitrary, model-prompt, and validation failures are not startup
retries. Once a session is ready, script validation still loops up to
`_AUTHOR_RETRIES + 1` = 3 attempts and feeds the validation errors back on each
retry. `_strip_fence` peels only the opening fence line and a trailing fence,
never splitting on every ``` , because a literal triple backtick inside the
script body would otherwise truncate it mid-statement.

Before generation, authoring searches the saved global definition library and
supplies up to three relevant local examples. The model may adapt one and declare
its exact `id@revision` in `META["adapted_from"]`; otherwise it authors from
scratch. A named saved workflow is never re-authored or reinterpreted.

Authoring runs in a **fresh, isolated, ephemeral** session (`wf-author:<id>`).
`wf-author:` is a stateless SessionManager prefix, so acquisition never consults
or writes a resume mapping. Every failed startup attempt is explicitly destroyed
before retry backoff, and the successfully acquired session is explicitly destroyed
when authoring finishes; destruction shuts down the provider, removes the registry
entry, and deletes any stale SessionMap entry. A workflow's authoring context
therefore never pollutes (or is polluted by) chat, consolidation, or another run. It
uses the tool-less `kirocrew-lite` agent with `ToolApprovalPolicy.REJECT_ALL`: the
dominant cold-start cost is loading the full MCP toolset and system prompt, and
authoring is pure text generation, so lite is what makes a fresh session cheap.
`REJECT_ALL` is belt-and-suspenders against an alternate ACP backend injecting tools
without `set_mode`.

The authoring system prompt (`service._AUTHOR_SYSTEM`) is the model-facing
statement of this contract: the required module shape, the sandbox rules, the
async-vs-sync split, the "results can be `None`, bind and guard" rule, the
host-owned `Budget` binding and caller-supplied `budget_total`, the exact
builtin allowlist, and guidance to keep agent count lean (a generator plus critic
pair per facet, not one verifier per claim). It must stay consistent with
`validate.py` and with the wired port set.

`start_from_intent` authors **inside** the run instead: the run is scheduled
immediately, `run_started` is emitted so it appears live, and authoring becomes a
visible `"Authoring"` phase whose progress streams as `log` events. That is why
`workflow_run(intent=...)` returns a `run_id` instantly rather than blocking an
HTTP request on a slow synchronous author.

### Agent execution adapters

The runner takes `agent_fn` as an injection, so it is testable against stubs and
never spawns `kiro-cli` in tests. Two production adapters:

- **`agent_exec.build_agent_fn`** (per-call sessions). Each `ctx.agent()` call gets
  a fresh isolated session keyed `wf:{run_id}:{i}` (a per-run counter, so each run
  restarts at `:0`), released with `cleanup=True` afterwards. A `session=<key>`
  call reuses the named session, which persists so a stateful chain keeps its
  history. Each step runs through `llm_helpers.stream_and_collect` with
  `AUTO_APPROVE` and `max_turns=_MAX_TURNS_PER_STEP`, then the output passes
  through `redact_credentials` and `redact_exfiltration_urls` before it can reach a
  run record, history, or parent chat. A per-turn usage row is persisted with
  `surface="workflow"` on a best-effort basis, with deliberately **narrow** guards:
  one wide try around import plus context read plus persist would let a single
  import failure silently drop every row from the workflow surface.
- **`agent_pool.build_pooled_agent_fn`** (warm sessions; the service default,
  `pool_agents=True`). Per-call sessions make every call pay a full cold start
  (subprocess spawn plus ACP `initialize` plus `session/new`, which loads the MCP
  toolset and system prompt), so an 8-agent run pays 8. The pool reuses the generic
  `acp.worker_pool.WorkerPool` to keep a small set of warm sessions keyed
  `wf-pool:{run_id}:{worker_id}`. Isolation is preserved because the engine hands
  each **concurrent** task a distinct worker; a **sequential** task reuses an idle
  worker after `provider.new_conversation()` (a fresh `session/new` on the live
  process, skipping spawn and initialize), falling back to a hard
  `SessionManager.reset` if that is unavailable or fails, so a reused worker can
  never carry prior-task context forward. Dead workers are retired and replaced.
  A `session=<key>` call bypasses the pool entirely, since it needs a stable named
  session rather than a reset-between-uses worker.

  Per-call `agent` / `model` / `cwd` overrides each get their **own** warm
  sub-pool, so a multi-specialist fan-out still gets warm reuse per specialist and
  a worker built for one identity never serves a call that asked for another.
  Distinct identities are capped at `max_identities` (default 8); beyond that a
  call runs on a one-shot unpooled `wf-unpooled:{run_id}:{i}` session, so total
  live workers stay bounded at `(max_identities + 1) * max_workers`. Pool teardown
  (`shutdown`) releases then `destroy()`s each session: `release(cleanup=True)`
  alone would not reap a `wf-pool:` key (its file-cleanup branch only fires for
  `subagent:` keys), leaking a warm `kiro-cli` process across runs.

  `_MAX_TURNS_PER_STEP` is imported from `agent_exec` rather than duplicated, so
  one edit retunes both paths; `test_workflows_agent_pool.py` pins them equal.

Named workflow sessions retain their provider and conversation, not their turn
lease. Both adapters release every successfully acquired named lease in a
`finally` block with `cleanup=False`, on success, exception and cancellation.
A failed or cancelled acquisition must not release a lease held by another
caller. A later call on that same name reuses the retained history.

Pool init failure is caught and falls back to `build_agent_fn`, so pooling can
never break a run start. The runner's `on_complete` hook fires on every exit path
(success, failure, cancellation) to shut the pool down, so warm sessions are always
released.

- **`agent_pool.admitted_agent_fn`** (the task-queue wrapper, applied by
  `WorkflowService._runner` to WHICHEVER of the two adapters above it chose,
  when a `RunnerAdmission` is attached). Each `ctx.agent()` call becomes the row
  `workflow:{run_id}:agent{n}` (`kind=workflow_agent`, `params={run_id, call,
  agent, session, lane}`, `provider=<model override>`, class `unknown`):
  written before the call runs (write-before-ack), admitted through the lane
  (memory-pressure defer, effective-cap slot, lease + generation), marked
  `running`, and settled `done` / `failed` / `cancelled` from the outcome. That
  mark is a FENCE: `admit` committed `starting` one statement earlier, so a
  refusal is a newer owner or a store outage, and the call does not run under a
  row that reaches no WAITING state (the dependency park below could not
  persist) — the row is failed and the wrapper raises `RunnerAdmissionRefused`
  (`taskq.md` § Every store write whose result is DISCARDED). The
  wrapper is a coroutine on the gateway's loop, so every one of those writes is
  off-loop: `accept_async`, `admit` (which routes its own store touches through
  `_db`), `running_async`, `done_async` / `fail_async`. The `except
  CancelledError` arm keeps the SYNCHRONOUS `cancel` -- an `await` there can be
  interrupted before the write is submitted, and a dropped terminal write leaves
  the row active for the next boot's reconciler. That arm only covers a cancel
  landing on the CALL; a run cancel or the wall-clock ceiling arriving while the
  call is still waiting for a lane slot, or while it is parked in
  `waiting_dependency`, is settled `cancelled` by the admission itself
  (`taskq.md` § Runner adapters), so a cancelled run leaves no `workflow_agent`
  row behind and none is left `queued` for a dispatcher this kind does not have.
  A
  dependency error the adapters recognise parks the row in `waiting_dependency`
  with its slot released and re-runs the call on the wake, at most
  `DEFAULT_MAX_ATTEMPTS` times; terminal signals fail it. The lane for a run is
  its launching `session_key`, or `system` when the run has none or was
  launched by a cron / hook (`lane_for`). Pinned by the taskq tests in
  `test_workflows_agent_pool.py` (cap beneath `max_workers`, a mid-run cap
  change, `fixed` mode, rows per call, failure and rate-limit settlement, a
  cancel while queued for a slot, a cancel while parked on a dependency, a call
  whose row descends from the slot holder, and the attach sweep over a row that
  was accepted and never claimed).

The gateway pins workflow agent concurrency at **4** on purpose, rather than
sizing it from `resolve_max_subagents()`: because the pool keeps a separate
sub-pool per identity with an aggregate bound of `(max_identities + 1) *
max_workers`, an auto-sized cap would raise worst-case resident `kiro-cli` workers
from 9x4 = 36 to 9x`subagent_auto_max` and OOM the gateway on a large host. The
run **ceiling** is unaffected by that and is config-driven.

### HTTP surface

Registered in `dashboard/server.py`, handled in
`dashboard/handlers/workflows.py`. These back both the chat `workflow_*` MCP tools
(which call them with `X-Internal-Secret`) and the Workflows dashboard tab; the
caller's `X-Session-Key` header becomes the run's `author` and `session_key`.

Before author, source run, intent run, saved-definition run or subtree rerun
calls the service, ordinary transport authentication and owner/app permissions
apply. The session's canonical execution record is captured before asynchronous
dispatch; missing or malformed member identity refuses instead of selecting Global.
Run list/detail/cancel/rerun keep ordinary execution permissions. A permitted
rerun retains the original run's member/store and strictest privacy mode, even
when requested from another member. Member stores add no separate cross-member ACL.

| Route | Body / params | Response |
|-------|---------------|----------|
| `POST /api/workflows/author` | `{intent}` | `{ok, source, meta}` or `{ok:false, errors}` |
| `POST /api/workflows/run` | `{source, args?, name?, budget_total?, timeout_secs?}` | `{run_id}` or `{error}` (400) |
| `POST /api/workflows/run_intent` | `{intent, args?, name?, budget_total?, timeout_secs?}` | `{run_id}` immediately |
| `GET /api/workflows/runs` | | `{runs: [...]}` compact, newest first |
| `GET /api/workflows/runs/{run_id}` | | full snapshot incl. `events`; adds `plan` under `?plan=1` when one is readable (404 if the run is absent) |
| `POST /api/workflows/runs/{run_id}/promote` | `{name?, description?, slug?}` | save exact source from a finished run or paused TaskRunner plan; 404 when unknown, 409 when not promotable or only a restored redacted source remains |
| `POST /api/workflows/runs/{run_id}/cancel` | | `{run_id, cancelled}` |
| `POST /api/workflows/runs/{run_id}/rerun` | `{from_index?, source?}` | `{run_id, from, replayed_before, edited}`; 400 on an invalid edited script, 404 on an unknown run |
| `GET /api/workflows/definitions?q=` | optional local match query | `{definitions: [...]}` |
| `POST /api/workflows/definitions` | `{source, format?, name?, description?, slug?, derived_from?}` | explicitly save a validated definition; format defaults to `python` |
| `GET /api/workflows/definitions/{id-or-slug}` | | one definition including revisions and lineage |
| `PATCH /api/workflows/definitions/{id-or-slug}` | `{source, expected_revision, name?, description?, slug?}` | append a revision; 404 when unknown, 409 on stale revision |
| `POST /api/workflows/definitions/{id-or-slug}/run` | `{input?, args?, budget_total?, timeout_secs?}` | run the exact saved revision; 404 when unknown, 409 on execution admission rejection, 503 when its executor is unavailable |

Every mutation route that accepts JSON requires a top-level object. A syntactically
valid scalar or array is rejected with 400 and `code: invalid_json` instead of
reaching field access and surfacing as an internal-server error.

With no `workflow_service` on state, every route answers 503.

Every response body passes through `_redact_obj`, which redacts dict **keys** as
well as values: agent output is parsed straight into these structures, so a
credential can arrive as a mapping key, and a values-only walk would pass it
through untouched. Two credential-shaped keys can collapse into one redacted key;
losing a pathological key beats leaking the secret.

`_opt_int` coerces `timeout_secs`, rejecting `bool` and any non-`int`, so a
malformed value becomes `None` ("no override") and can never widen or remove a
run's ceiling.

Live progress reaches the browser as a `workflow_run_event` WS broadcast carrying
the redacted event plus the run's `session_key`, and the frontend
(`website/src/apps/workflows/runModel.ts`) folds the stream into a phase tree and a
budget snapshot. On a terminal state, `dashboard/workflow_inject.py` posts the
result into the originating chat slot and starts (or queues) an agent turn so the
user gets a synthesized answer rather than a raw blob.

### Plan preview and the graph view

The event stream says what a run DID. `workflows/preview.py` says what its script
SAYS it will do, so a run can be drawn as a flow chart before its first agent
starts and lit up as it goes. `plan_from_source` reads the already-validated source
with `ast` and returns `{phases: [{title, certain, nodes: [{kind, label, certain}]}],
truncated, titleLimit}`, or `None` when no plan is readable. `None` and an empty plan
are deliberately different: the run-detail response then OMITS `plan` rather than
sending null, so the view can say "no plan" instead of drawing an empty one. A
task-plan source lands there, having no `workflow(ctx)` entrypoint.

Deriving the plan parses the script, and only the graph mode draws it, so it is
**opt-in**: `GET /api/workflows/runs/{run_id}` reads it only under `?plan=1`. The run
panel polls that endpoint every couple of seconds, and the tree mode must not pay for
a parse it never renders.

Every retained field is bounded. `MAX_PLAN_PHASES` / `MAX_PLAN_NODES` bound how MANY
rows come back and set `truncated`; `MAX_PHASE_TITLE_CHARS` bounds a phase title, and a
node label is bounded by whichever cut the RUN will apply to it. A literal `label=` is
the runner's own label, which it never cuts, so `MAX_LABEL_CHARS` bounds it. A label
that falls back to the prompt is cut by the runner at `RUNNER_PROMPT_LABEL_CHARS`,
because the runner emits `label or prompt[:40]` -- and each word of that expression is
mirrored: a falsy `label=""` loses to the prompt exactly as a missing one does, and
`prompt` is read under both its positional and keyword spellings. Drawing a label the
run will not emit makes a planned box rename itself the moment it lights up. Both cuts go through `_bounded`, which
**redacts first and cuts second**. The order is load-bearing: the response-level
redactors match a credential by its full shape, so a cut landing inside one leaves a
prefix they no longer recognize -- measured against the real redactor, 33 of a
40-character `ghp_` token and 19 of a 20-character AWS key id survive a cut-then-redact
order. A title is cut before anything retains or compares it, so phase reuse and the
stored row cannot disagree. The bound travels as
`titleLimit` because the consumer pairs a plan row with a `phase_started` event by
title and the runner emits that title whole: without the number, a cut title would
read as a phase the plan missed.

An `unknown` node is the whole safety property. A construct whose shape depends on a
runtime value is never guessed at: `if` / `for` / `while` / `try` / `match`, a
conditional expression, a comprehension, `ctx.parallel` over anything but a literal
list or tuple, `ctx.pipeline`, `ctx.workflow`, a `ctx.phase` whose title is not a
literal, and
a call into a helper the script defines each contribute exactly one `unknown` node
naming the construct, and every node found inside such a region is `certain: false`.
`MAX_PLAN_PHASES` / `MAX_PLAN_NODES` bound the output and set `truncated`.

That property has a precondition: the previewer must know every `ctx` method the DSL
offers. `MODELLED_CTX_METHODS` holds the ones it draws and `NARRATION_CTX_METHODS` the
ones that draw nothing on purpose, and together they cover the `WorkflowContext`
Protocol exactly. `test_workflows_conformance.py` freezes that Protocol, but freezing
the CONTRACT says nothing about whether the previewer classified a method added to it,
so a deliberate re-freeze could introduce a work-spawning verb the previewer silently
ignores. Two things close that: a parity test derived from the Protocol (never a
hand-written copy of it, which is the enumeration that would go stale), and
`_visit_call` degrading an unclassified method to an `unknown` node rather than drawing
nothing -- honest at run time even if the pin is ever relaxed.

`website/src/apps/workflows/planModel.ts` merges the plan with the stream. Both inputs
are coerced to text at that seam, because nothing between a script and the graph
guarantees a string: `ctx.phase(123)` records a numeric `title`, `ctx.agent(...,
label=123)` a numeric `label`, and `runModel` reads event fields with a cast and no
runtime narrowing -- deliberately, since narrowing there would change what the tree view
renders for a malformed event. The graph does string work on those values (cutting a
title to the plan's limit, sanitizing a label), so an uncoerced number reached `.slice`
and blanked the whole view rather than making one node read oddly. The coercion is total
and lives only at the two entry maps; the title cut is not a second coercion point,
because no input can reach it uncoerced. Ordinal
pairing is what makes planned node *i* the same work as actual node *i* — the
script's calls run in source order — and an `unknown` node is a FENCE that stops it,
because past an unpredictable region no position means anything. Past the fence the
view shows the markers, then reality once reality exists, so a prediction never sits
beside the thing that superseded it. A marker whose region HAS materialized carries
`resolvedCount`, and the view then says how much ran there instead of still saying the
shape is undecided — a marker that keeps asking beside real boxes cannot be told from
one still being waited on. Run status and plan provenance are separate
fields: an unpredicted agent that failed is both, and the failure matters more.

`WorkflowRunGraph` is a MODE of the run panel, not a second tab, so the graph reads
the one snapshot the panel already fetches. It is deliberately **not a control
surface** — nothing in the drawing is clickable. Wiring a node to
`workflow_rerun_subtree` would let a misclick spend tokens and restart real agents;
rerun stays on the run controls, where the control names what it does.

Per-node token cost is absent by necessity, not by choice: the stream carries a
run-level budget only (`run_started.budget_total`, `budget_update.spent`) and no
per-agent cost attribution, so drawing one would mean inventing a number. Per-node
TIMING needs no new data, which is why it shipped first (#11795): `agent_started`
and `agent_finished` each already carry a `ts`.

### MCP tools

`mcp_core.py` exposes eight tools that forward to the routes above:
`workflow_author`, `workflow_run` (takes `source`, `intent`, or an exact saved
`workflow` reference, plus `input`, `name`, `args`, `budget_total`),
`workflow_library_list`, `workflow_status`, `workflow_result`, `workflow_list`,
`workflow_cancel`, and `workflow_rerun_subtree`. All share one exit path that
redacts LLM-derived strings. Every workflow write resolves the caller with
`_resolve_session_key_strict()` and passes that verified identity to the HTTP
write, so a subagent cannot inherit an ancestor session and inject completion
into the parent's chat. Saved-definition, ad-hoc `source` and `intent` runs all
refuse before HTTP when strict identity is unavailable, even if the lenient
resolver finds an ancestor session. Authoring, cancellation and subtree reruns
use the same strict identity rule. Durable create and update operations
are deliberately absent from the model-facing MCP surface: only the dashboard's
explicit human management and completed-session confirmation flows may call the
mutation routes.

The dashboard handles `/workflow <slug> [input]` locally before harness session
acquisition; `/workflow` alone lists saved definitions. This keeps the command
identical across Kiro and adapted harnesses, and prevents an explicit reference
from being reinterpreted by a harness. The definition format selects its owning
driver; there is no user-facing engine choice. The Agent Capabilities →
Workflows tab is the human management surface. Its Workflow library view owns
listing, authoring unsaved drafts, lineage, source edits as new revisions, and
exact saved runs; its Runs view owns common history and explicit promotion.

The `workflows` builtin app (`apps/builtins/workflows/`) is `defaultEnabled:
false` and `hidden: true`; it exposes `/validate`, `/run` and `/examples` over its
own stdlib HTTP server.

`/examples` serves the scripts in [`examples/workflows/`](examples/workflows/),
which `server.py::_examples_dir()` locates by walking up from the module toward the
repo root. It resolves only in a source checkout: top-level `docs/` is not packaged,
so an installed gateway serves an empty list and the dashboard hides the example
picker.

## Audit (SEL)

`runner._default_audit` writes each workflow audit record to the SEL security
event log via `sel().log_tool_invocation` with `source="workflow"`,
`tool_kind="workflow"`, `tool_name=f"workflow.{event_type}"` and
`request_id=run_id`. Three record types:

| `event_type` | When | Fields |
|--------------|------|--------|
| `run_started` | before any `exec`, on every run | `author`, `runner`, `arg_keys` (key names only, never values), `script_hash`, `outcome="started"` |
| `agent_call` | after each `ctx.agent()` settles | `author`, `runner`, `agent_id`, `call_index`, `outcome` (`ok`/`failed`), `has_schema`, `error` |
| `run_finished` | on success only | `author`, `runner`, `outcome="ok"`, `result_hash`, `agent_calls` |

`result_hash` is a 16-hex-char sha256 prefix of the JSON-serialized result, never
the raw data.

The sink is injectable (`audit=`), and every sink is wrapped by
`_guarded_audit` so a **raising** sink can never break a run: `_default_audit`
guards itself, but an injected sink may not, and wrapping at assignment makes every
call site safe without a try/except at each one.

## Configuration

| Key | Default | Meaning |
|-----|---------|---------|
| `agent.workflow_run_timeout_secs` | 3600 | default wall-clock ceiling per run; clamped to 60..21600 by `clamp_run_timeout`, so it can be raised for long multi-phase investigations but never disabled |

Workflow agent concurrency is **not** configurable: it is pinned at 4 in
`dashboard/server.py` for the pool-bound reason above.

## Changing this contract

`workflows/__init__.py` says it plainly: **changing a signature or an event shape
is a re-freeze.** Every module in the package, the Workflows frontend page, and
the whole `test_workflows_*` suite fan out from that contract, so a silent change
breaks consumers with no warning.

A re-freeze must update, in the same change:

1. `src/kiro_crew/workflows/__init__.py` (the contract itself),
2. this spec, and
3. **`test/test_workflows_conformance.py`** (the conformance test).

That test is the canary. It asserts `__all__` exactly, the `WorkflowContext` data
attributes exactly, the ctx **method set** exactly, each ctx method's async-ness
and full parameter list (names, kinds, defaults, including `self` so a dropped
leading positional is caught), each Port protocol's `@runtime_checkable` flag and
method signatures, and the `EVENT_TYPES` tuple with the JSON envelope round-trip.

If a case in it turns red and you did **not** intend a re-freeze, revert the
contract edit. Do not "fix" the expectation.

The neighbouring suites carry the same rule for their own invariants: never relax a
`validate.py` check without updating `test_workflows_invariants.py` and the
`test/workflows/malicious/` corpus, and never relax the layering without
`test_workflows_architecture.py`.

The gate labels cited by the engine's docstrings and by the `test_workflows_*`
docstrings (A4, B5, C1, D1, F1, F2, G1, …) are catalogued under
[Conformance gates](#conformance-gates) below, which names the test pinning each.
The sections above state those invariants directly rather than by label, so the
catalog is a lookup table for the ids, not a second contract.

> Open question: `M<n>` milestone markers (`M5`, `M6`, `M6.7`, …) still appear on
> comments and docstrings across every workflow surface — the engine's tests, the
> MCP tools, the validators, the gateway wiring, and the Workflows UI. They are
> delivery markers, not gates, so nothing defines them, and
> `grep -rnE '\bM(5|6)(\.[0-9]+)?\b'` is the live list rather than an inventory
> kept here, which would go stale on the next edit. Clearing them belongs to each
> file's own pass.

## Conformance gates

A *gate* is one named invariant of this engine that a test enforces as a failing
build rather than as prose. The engine's docstrings cite gates by bare id
(`GATE F2`, `GATES A3, A5`, `C3 schema-violating object rejected`), so the ids need
a definition a reader can look up; the tables below are that lookup, and every row
is derived from the test that pins it. A gate is closed by its test, not by this
table: where a row and its test disagree, the test is right.

### How to read a row

- **Guarantees** is the property that goes RED when broken, not the
  implementation.
- **Pinned by** names the test module and the test function(s). Test modules live
  at `test/` in the repo root; engine sources live at
  `src/kiro_crew/workflows/`.
- A gate is *closed* by its test, not by this document. If a row disagrees with
  the named test, the test is right.

The letter series are **not contiguous**: only the ids listed here appear in the
code. Do not infer an `A1`, `A2`, `A6`, or `B8` from the gaps. Ids of the form
`M<n>` (for example `M4`, `M6.2`) also appear in workflow docstrings; those are
delivery milestone markers, not gates, and have no entry here.

### Group A: execution semantics

| Gate | Guarantees | Pinned by | Constrains |
|---|---|---|---|
| A3 | `pipeline()` has NO barrier between stages (an item may reach a later stage while another item is still in an earlier one), while `parallel()` IS a barrier (it awaits every thunk before returning). | `test_workflows_dsl.py::test_pipeline_no_inter_stage_barrier_deadlock_proof`, `::test_parallel_is_a_barrier` | `dsl.py` |
| A4 | `Budget` is a HARD ceiling: `charge()` that reaches or passes `total` raises `BudgetExceeded`; `total=None` means unbounded and `remaining()` is `inf`. | `test_workflows_context.py::test_budget_hard_ceiling_raises_at_total`, `::test_budget_hard_ceiling_raises_over_total`, `::test_budget_unbounded_when_total_none` | `context.py` (`Budget`) |
| A5 | A failing thunk or pipeline stage resolves to `None`; the combinator itself never raises, so callers filter `None` instead of handling exceptions. | `test_workflows_dsl.py::test_parallel_failed_thunk_becomes_none_and_does_not_raise`, `::test_pipeline_failed_stage_drops_item_to_none` | `dsl.py` |
| A7 | A run emits exactly the documented event vocabulary, in order, with contiguous `seq` from zero: `run_started` first, one of `run_finished` / `run_failed` / `run_cancelled` last. `REQUIRED_DATA_KEYS` covers exactly `EVENT_TYPES`, and the validator rejects an unknown type or a missing required key. | `test_workflows_events.py::test_required_keys_cover_exactly_event_types`, `::test_every_event_type_has_a_builder_and_validates`, `::test_seq_is_monotonic_from_zero`, `::test_validate_rejects_unknown_type`, `::test_validate_rejects_missing_required_keys`; `test_workflows_runner.py::test_happy_path_emits_full_stream_in_order`, `::test_run_started_first_and_finished_last` | `events.py`, `runner.py`, `__init__.py` (`EVENT_TYPES`) |

### Group B: sandbox, ceilings, and audit

Group B splits into a **static half** (`validate.py` refuses the construct before
the script is ever compiled) and a **runtime half** (`context.build_safe_globals`
makes the capability absent from the exec namespace, so a construct that somehow
slipped past static validation still cannot reach anything). B3 has both halves
by name; the two together are why a single missed AST pattern is not an escape.

| Gate | Guarantees | Pinned by | Constrains |
|---|---|---|---|
| B1 | A workflow script may not `import` anything, and may not reference `eval` / `exec` / `compile` / `open` / `__import__` / `globals` / `locals` / `getattr` / `setattr` / `vars` / `input` / `__builtins__`. The rejection message names the offending symbol. | `test_workflows_invariants.py::test_b1_imports_rejected`, `::test_b1_forbidden_builtins_rejected` | `validate.py` |
| B2 | No dunder attribute or name access (`().__class__`, `__builtins__`, and the rest), no private (`_`-prefixed) attribute access, and no `.format` / `.format_map` call — their template is interpreted at run time, so a traversal can be assembled from parts no static fold can resolve (f-strings are the replacement: their fields are real AST and are already checked). An inline adversarial-escape corpus is rejected wholesale. | `test_workflows_invariants.py::test_b2_dunder_attribute_rejected`, `::test_b2_adversarial_escapes_rejected` | `validate.py` |
| B3 | The nondeterminism modules (`time`, `random`, `uuid`) are unreachable, statically (they cannot be imported) and at run time (no `__import__` in the sandbox namespace, and `SAFE_BUILTINS` excludes nondeterministic and I/O builtins). Determinism is what makes a run stream resume-stable. | static: `test_workflows_invariants.py::test_b3_determinism_modules_rejected`, `::test_b3_safe_builtins_exclude_nondeterminism_and_io`; runtime: `test_workflows_context.py::test_import_statement_fails_in_safe_globals`, `::test_hostile_snippet_fails_at_runtime_in_safe_globals` | `validate.py`, `context.py` (`build_safe_globals`) |
| B4 | Event persistence is JSON only: `serialize_events` / `deserialize_events` round-trip through `json`, reject non-JSON and non-array input, and the module imports no `pickle` / `marshal` / `shelve`. | `test_workflows_events.py::test_round_trip_through_json`, `::test_serialize_output_is_pure_json`, `::test_deserialize_rejects_non_json`, `::test_events_module_has_no_pickle_import` | `events.py` |
| B5 | A wall-clock timeout terminates a runaway run and reports it as a clean `run_failed` with `where == "ceiling"` and `error == "timeout"`. The guard must never let an `asyncio.CancelledError` escape `run()` to the caller. | `test_workflows_runner.py::test_wall_clock_timeout_kills_runaway`, `::test_timeout_never_leaks_cancellederror` | `runner.py` |
| B6 | `AgentCounter` caps lifetime `ctx.agent()` calls per run (default `DEFAULT_MAX_AGENTS_PER_RUN = 1000`) and the cap is enforced through the runner: an unbounded agent loop stops at the limit and ends as `run_failed` at the ceiling. | `test_workflows_context.py::test_agent_counter_raises_past_limit`, `::test_agent_counter_default_limit`; `test_workflows_runner.py::test_agent_count_cap_enforced` | `context.py` (`AgentCounter`), `runner.py` |
| B7 | The exec namespace exposes only `SAFE_BUILTINS` plus `ctx`, so a script has no filesystem or egress reach: `open`, `eval`, `exec`, `compile`, `__import__`, `input` and `getattr` are absent, and benign safe builtins still work. | `test_workflows_context.py::test_safe_globals_only_exposes_ctx_and_safe_builtins`, `::test_hostile_snippet_fails_at_runtime_in_safe_globals`, `::test_safe_builtins_actually_usable` | `context.py` (`build_safe_globals`) |
| B9 | Every hostile script in the on-disk escape corpus (`test/workflows/malicious/*.py`) is statically rejected, with a non-empty error. The corpus directory must exist and be non-empty, and a new escape idea is added by dropping a file in it: the test parametrizes over the directory. | `test_workflows_malicious.py::test_every_malicious_script_is_rejected`, `::test_corpus_dir_exists_and_is_populated` | `validate.py`, `test/workflows/malicious/` |
| B10 | Every run writes an audit trail (author, runner, arg KEYS only, one record per agent call with its outcome, and a result *hash*, never the raw result). The sink is injectable and a sink that raises can never fail the run. | `test_workflows_audit.py::test_run_emits_started_and_finished_audit_with_author`, `::test_each_agent_call_is_audited`, `::test_result_hash_never_leaks_raw_result`, `::test_failed_agent_audited_as_failed`, `::test_audit_failure_never_breaks_run` | `runner.py` (`_default_audit`, `_result_hash`), [sel.md](sel.md) |

### Group C: structured output for `ctx.agent(schema=)`

The runtime ships no `jsonschema`, so `schema.py` is a dependency-free validator
for the JSON-Schema subset the DSL uses. C1 to C3 are the three outcomes that
subset has to get right.

| Gate | Guarantees | Pinned by | Constrains |
|---|---|---|---|
| C1 | A conforming object validates clean (empty error list) and is returned to the script. | `test_workflows_schema.py::test_c1_valid_object_passes` | `schema.py` (`validate_against_schema`) |
| C2 | Malformed or invalid model output triggers a *bounded* retry (`DEFAULT_SCHEMA_RETRIES = 2`, so at most initial plus 2 attempts), then returns `None` rather than raising or looping. | `test_workflows_schema.py::test_c2_retry_then_success`, `::test_c2_all_malformed_returns_none`, `::test_c2_schema_violation_retried_then_none` | `schema.py` (`run_with_schema`) |
| C3 | An object that parses as JSON but violates the schema is rejected, not returned: missing `required` key, wrong type, `enum` violation, and `bool` not counting as `integer`. | `test_workflows_schema.py::test_c3_missing_required_rejected`, `::test_c3_wrong_type_rejected`, `::test_c3_enum_violation_rejected`, `::test_c3_bool_is_not_integer` | `schema.py` |

### Group D: Kiro Crew's own `ctx` primitives

Each native primitive delegates to a port injected per run. The gate is the
*calling convention* (the ctx surface reaches the port with the right arguments),
not the real service wiring, which is the gateway's job. A primitive whose port
is not wired fails the run with a message naming the primitive instead of
crashing.

| Gate | Guarantees | Pinned by | Constrains |
|---|---|---|---|
| D1 | `ctx.cron` reaches the cron port with the job name, cron expression and workflow name. | `test_workflows_native.py::test_d1_cron_port_invoked` | `runner.py`, `__init__.py` (`CronPort`) |
| D2 | `ctx.nudge` reaches the nudge port with the run's originating `session_key` threaded through, plus a notify emitter so nudge outcomes surface in the run event stream. | `test_workflows_native.py::test_d2_nudge_port_invoked` | `runner.py` |
| D3 | `ctx.memory.get/set` persist across a run through the memory port, and `ctx.learn.add` reaches the learn port with its default `scope="workspace"`. | `test_workflows_native.py::test_d3_memory_get_set_and_learn` | `runner.py`, `__init__.py` (`MemoryPort`, `LearnPort`) |
| D4 | `ctx.approve` awaits the approval port and returns its boolean decision to the script. | `test_workflows_native.py::test_d4_approve_resolves_decision` | `runner.py` |
| D5 | `ctx.send_slack` and `ctx.send_message` reach their ports in call order with the resolved target (`ctx.owner_dm` resolves to the run's owner) and text. | `test_workflows_native.py::test_d5_send_slack_and_message` | `runner.py` |
| (all D) | An unwired port produces `run_failed` with the primitive's name in the error, not a `NotImplementedError` traceback. | `test_workflows_native.py::test_unwired_primitive_fails_cleanly` | `runner.py` |

### Group E: dashboard tab (unpinned)

| Gate | Guarantees | Pinned by | Constrains |
|---|---|---|---|
| E1 | Undocumented. The only statement of intent is an inline comment in `website/src/apps/workflows/WorkflowsPage.tsx` ("invalid script blocks the run") on the run handler, which validates before it runs. No test asserts it by id. | Nearest coverage: `website/src/test/WorkflowsPage.test.ts` (pure event-stream folding), `website/playwright/builtin-apps.spec.ts` (`/workflows` renders). | `website/src/apps/workflows/WorkflowsPage.tsx` |
| E2, E3, E4 | Undocumented. These ids appear only inside the range `E1-E4`, described as Playwright gates against a dev instance; no individual id is defined or asserted anywhere in the tree. | See `website/src/test/WorkflowsPage.test.ts` (which names the range) and `website/playwright/builtin-apps.spec.ts`. | `website/src/apps/workflows/` |

The backend half of the tab is covered without gate ids, in
`test_workflows_app.py` (manifest shape, `handle_validate` / `handle_run` /
`handle_examples`, and redaction of credentials and exfiltration URLs before a
run payload leaves the handler) and `test_workflow_handler_json_contract.py`
(every legacy mutation handler rejects a non-object JSON body with a coded 400
before service dispatch, while object bodies still reach that service).

### Group F: fitness gates on the engine itself

Group F gates are enforced as pure-stdlib AST scans with no `import-linter` or
`git` dependency, so they hold in any checkout: git state is environment-fragile
and a git-dependent gate can redden a clean trunk.

| Gate | Guarantees | Pinned by | Constrains |
|---|---|---|---|
| F1 | The layering holds: `validate` / `dsl` / `schema` / `events` / `registry` / `__init__` and the optional adapters (`agent_exec`, `agent_pool`, `store`, `library`) are leaves with no intra-package siblings; `context` may import `validate`; `runner` may import `validate`, `dsl`, `events`, `context`, `schema`, `registry`; `service` sits above the runner. No module may import backwards, no module escapes the declared contract, and the engine must not reach into `kiro_crew.dashboard.state` or `kiro_crew.dashboard.ws` (progress goes through the event bus or a port). | `test_workflows_architecture.py::test_layering_no_backward_or_unexpected_sibling_imports`, `::test_engine_does_not_import_dashboard_internals`, `::test_every_module_is_covered_by_the_contract` | all of `src/kiro_crew/workflows/` |
| F2 | The frozen contract in `workflows/__init__.py` cannot drift silently: `__all__`, the `WorkflowContext` data attributes, its exact method set, each method's signature and async-ness, each port's methods and `runtime_checkable`-ness, `EVENT_TYPES` (exact and ordered), and the event envelope keys plus JSON round-trip. Changing any of them is an explicit re-freeze that must update `__init__.py`, the spec above, and this test together. | `test_workflows_conformance.py::test_all_exports_exact`, `::test_ctx_method_set_exact`, `::test_ctx_method_signature`, `::test_port_method_signature`, `::test_event_types_exact_and_ordered`, `::test_event_envelope_keys_exact` | `__init__.py` |
| F3 | Every implementation module under `workflows/` is imported by at least one `test/test_workflows_*.py`, so a module cannot be added without a test that reaches it. The gate carries its own negative control, so it cannot silently stop catching orphans. | `test_workflows_presence.py::test_every_workflows_module_has_a_referencing_test`, `::test_presence_gate_flags_an_orphan_module` | all of `src/kiro_crew/workflows/` |

### Group G: authoring reliability

The DSL is only useful if an agent can author it on the first try, so authoring
reliability is a measured gate rather than an assumption. The candidate set mixes
the shipped examples (known-good output) with intentionally plausible-but-flawed
scripts, so the rate is a measurement and not a tautology.

| Gate | Guarantees | Pinned by | Constrains |
|---|---|---|---|
| G1 | The first-try valid-script rate over the candidate set is at or above `G1_TARGET = 0.80`, every shipped example validates, and each deliberately flawed candidate is actually rejected. | `test_workflows_authoring_eval.py::test_g1_shipped_examples_all_validate`, `::test_g1_first_try_valid_rate_meets_target`, `::test_g1_flawed_candidates_are_caught` | `validate.py`, the shipped example scripts the test resolves |
| G2 | Every script that validates terminates cleanly against a stub provider: `run_started` first, `run_finished` or `run_failed` last, `seq` contiguous. It never hangs, and no exception escapes the runner (a run that ends `run_failed` still satisfies G2, which is about termination and stream shape). | `test_workflows_authoring_eval.py::test_g2_valid_scripts_run_to_completion`, `::test_g2_simple_authored_script_fully_succeeds` | `runner.py`, `validate.py` |
| G3 | Author-session startup retries only transient ACP startup failures, uses a fresh isolated key for each attempt, destroys a partial session before retrying, and preserves the final startup error when the bounded attempt budget is exhausted. Successfully acquired author sessions are stateless and destroyed after authoring, so their provider, registry entry, and SessionMap entry do not survive. Arbitrary failures are not retried. | `test_workflows_service.py::test_author_retries_transient_startup_with_fresh_session`, `::test_author_destroys_partial_session_before_startup_retry`, `::test_author_does_not_retry_arbitrary_startup_failure`, `::test_author_startup_retry_exhaustion_preserves_last_error`, `::test_author_uses_isolated_destroyed_lite_session` | `service.py`, `session.py` |

### Adding or changing a gate

1. Write the test first: a gate is the test, and this table is its index.
2. Cite the gate by id in the source docstring it constrains, and add the row
   here in the same change.
3. Never relax a check to make a red gate green. A group-B case that flips from
   RED to GREEN because a validator check was loosened is a sandbox regression,
   not a fix; a red F3 means the new module needs a test, not an exemption.

## Related

| Topic | Spec |
|-------|------|
| Subagent spawning, caps, reaper | [subagent](subagent.md) |
| ACP session pool the adapters use | [session](session.md) |
| SEL event schema and integrity chain | [sel](sel.md) |
| Credential redaction, sandbox, denied commands | [security](security.md) |
| Cron scheduling behind `CronPort` | [learn-cron-dashboard](learn-cron-dashboard.md) |
| App manifest model for the Workflows app | [app-kit-platform](app-kit-platform.md) |
| Example scripts | [examples/workflows](examples/workflows/README.md) |

### Provider receipts and unavailable-store cancellation

Member workflow prompt construction passes the acquired provider and actual
resume state to `ContextBuilder.build_message(context_provider=...)`, after
best-effort `prepare_store_vectors`. V2 preparation opens only an existing
database and attaches the lazy embedding callable; it does not warm a model,
enqueue embeddings or search fragments. Healthy cold starts therefore retain
query-free scoped lessons. Failed preparation leaves manual essentials usable
and emits an unavailable-memory diagnostic; prompt construction never creates
the learned-memory facade or substitutes Global. Temporary skips preparation
and learned lessons. The provider's `EssentialDelivery` owns acknowledgment
of a productive, successful raw terminal. Author and pool `is_new` flags describe
lifecycle only; they never acknowledge essential delivery. Failed or cancelled
attempts retain the full candidate, and a new conversation has its own receipt.

Completion delivery reads the canonical context even when callers omit display
fields. Owner cancellation can use an existing run record when learned memory
is unavailable. Missing or malformed member identity does not become Global.

### Atomic run identity allocation

Before returning a new `wf_NNNNNN` identity, the service burns its number in
`<workflows dir>/.run-id.json`. The version-1
record has exactly `version` and `high_water`: both are strict integers, and
`high_water` is a nonnegative uint64. Booleans, floats, unknown versions and
out-of-range values refuse allocation. Six digits are a minimum display width;
`wf_999999` is followed by `wf_1000000`. The uint64 ceiling refuses, never wraps.

One permanent `.run-id.lock` inode serializes the complete read/check/write/sync
transaction with the platform's required exclusive cross-process lock. The
counter is atomically replaced; the lock is never replaced, truncated or deleted.
The service offloads the entire transaction in one `asyncio.to_thread` call.
Cancellation cannot release a still-running worker's lock. After a durable burn,
failed or cancelled admission, registry eviction and author-only calls never
make the ID available again. The recovered registry sequence is only a lower
bound, not a way to recreate lost allocator state.

First-use protocol, entirely under that lock:

1. An empty lock means initialization has not published its witness. Merely
   creating the lock does not enable the allocator. A second process may take
   the lock before its creator and finish initialization normally.
2. With no witness and no counter, persist the recovered lower bound. With an
   existing valid counter, retain and re-sync it instead. Use same-directory
   atomic replacement, owner-only access, file fsync and parent-directory fsync.
3. Only after the initial counter is durable, write byte `1` to the permanent
   lock and fsync it and the directory chain. Only then may an ID be allocated.
4. Select `max(high_water, recovered_floor) + 1`,
   atomically persist the selected number and sync its parent before returning.

A crash before witness publication leaves either no counter or a valid initial
counter; the next lock holder can finish initialization because no ID was issued.
A published witness with a missing counter fails closed. Empty, malformed or
unknown-version counters always fail closed, even during initialization. Invalid
witness bytes also refuse. Every allocation syncs the witness again so a failed
prior witness sync cannot be mistaken for completed initialization. I/O or lock
failure returns no ID and never rolls back a visible high water. A failure before
counter publication has not issued that candidate; a failure after publication
may burn it without returning it. Directory fsync retains `atomic_write`'s
existing platform limits, including Windows and filesystems without directory
sync support. This protocol does not detect restoring a syntactically valid old
backup or destroying both allocator records.

The counter is ordinary workflow allocation state and creates no per-run grant
or reservation registry. Allocation grants no memory or caller permission.
No legacy V2 identity migration, rollback or retirement path is introduced.

On Windows, allocator objects must belong to the current user or to the local
Administrators group (`S-1-5-32-544`), the default owner for elevated creation.
The group exception requires a confirmed local volume: the same SID on a network
share denotes that server's administrators, not this machine's. An unknown user
SID, unreadable owner or any other owner still refuses. The existing fail-loud
owner-only DACL lockdown remains required for both directories and files; no
ownership is rewritten. POSIX retains its exact-current-UID check.

Saved task-plan execution passes its captured execution context to TaskRunner,
including calls supplying only `author`. Runtime, worker and reviewer contexts
retain that record; closing the author cannot select Global for the saved work.

### What one in-process run costs, and the budget that awaits it

`finished()` in `test/test_workflows_private_execution.py` is the ONE wall budget
over a whole run in the in-process suite, shared by every module that imports it
(`test_workflows_run_identity.py`, `test_workflows_private_paths.py`), and its
size — `WORKFLOW_RUN_BUDGET_SECS` — comes from CI measurements, never from a
local run: a local run is not evidence about the runner that reds. One private
run of that module's `SCRIPT` (five `ctx.agent()` calls, the last two a named
`session=` chain) spends its wall in filesystem thread hops rather than in the
model — about 600 `asyncio.to_thread` round trips per run, because
`WorkflowScope.validate()` is four hops, `prepare()` is that plus two more, and
`prepare` runs TWICE per call by construction (the adapter's own, before
`get_or_create`, so the worker session inherits the store before its provider
exists, plus the one inside `WorkflowScope.prompt`), with `build_message` on the
embed pool on top. That class of work is what a 4-vCPU windows-latest runner
under `-n auto` is slowest at: measured ~6x its Linux cost, and 1.42x spread
across identical params inside a single job. A budget a HEALTHY run can exceed
there reports a cost as a hang, so the failure text names the pending call set
and the instant that set last changed. The CHANGE INSTANT is what separates the
two unconditionally: a capacity park shows a pending set that never moves, a slow
run one that moved inside the budget. Frame names cannot be relied on for it —
they differ for a lane park (`admit` → `acquire`) but CAN be identical for the
park that matters most here, a wedged store-writer hop, which shows the same
`to_thread` frame a merely slow run does.
