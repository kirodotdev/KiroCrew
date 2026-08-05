# Dynamic Workflows Module

## Overview

A **dynamic workflow** is one LLM-authored Python module that orchestrates several
Kiro Crew agents through ordered phases. The module declares a pure-literal `META`
dict and a single `async def workflow(ctx)` entrypoint; the engine validates it
statically, executes it in a restricted namespace under hard ceilings, and streams
a typed event journal that drives the UI, the chat `workflow_*` MCP tools, and
resume.

The subsystem lives in `src/kiro_crew/workflows/` (12 modules). This file is the
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
agent_exec, agent_pool          (leaves: no sibling imports, or __init__ only)
    ↑
context      (may import: __init__, validate)
    ↑
runner       (may import: __init__, validate, dsl, events, context, schema, registry)
    ↑
service      (may import: validate, registry, runner, agent_exec, agent_pool, store)
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
clock, which is what makes the event journal and the resume prefix stable. The
runner does use `time.monotonic()` in **host** code for the wall-clock guard and
the run duration; that is never exposed to the script.

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
| `ts` | `str` | timestamp, supplied by the caller (the runner passes `ctx.now`) |
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

`tests/workflows/malicious/*.py` is an adversarial escape corpus; every file in it
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

The check is **scope-aware**: only attribute accesses bound to the entrypoint's
context parameter are checked, so a helper whose own parameter happens to be named
`ctx` (say `def read(ctx): return ctx.get("k")`, called with a dict) is out of
scope. Helpers that receive the real context are under-enforced by design; their
misuse still fails at run time with the explicit unwired-port error.

## Structured output (`schema=`)

`schema.py` is a dependency-free validator for the JSON-Schema **subset** the DSL
actually uses. Kiro Crew's provider layer has no native schema enforcement and the
runtime ships neither `jsonschema` nor a JSON-Schema-consuming pydantic path.

Supported: `type` (`object`, `array`, `string`, `integer`, `number`, `boolean`,
`null`, or a union list of those), `properties`, `required`, `items`, `enum`.
Unknown keywords are ignored, so a richer schema still validates on the parts this
understands. `bool` is explicitly **not** an `integer` or `number`.

`parse_json(text)` is tolerant of the two shapes a model actually returns: a
fenced ` ```json ` block, and JSON wrapped in prose (it falls back to the
outermost `{...}` / `[...]` span, trying whichever delimiter appears first so a
prose-wrapped array yields the array, not an inner object). It never `eval`s.

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

## Run registry, persistence, and resume

### `registry.py`

`RunRegistry` is an in-memory, **loop-affine** bounded-LRU store of `RunHandle`
records: all mutation happens on the gateway event loop, so no locks are needed,
and snapshots handed to callers are plain JSON-serializable dicts, never the live
objects (in particular never the `asyncio.Task`).

`RunHandle` holds `run_id`, `name`, `status`, the growing `events` list, `result`,
`error`, `author`, `session_key`, `source`, `args`, `agent_results`,
`agent_errors`, and the driving `task`. Statuses: `running`, `finished`, `failed`,
`cancelled`.

Two distinct serializations:

- `snapshot(include_events=)` is the **UI view**. The compact form adds derived
  live progress (`phase` from the last `phase_started`, `last_log` from the last
  `log`) plus `partial_result_count` / `agent_error_count`; the full form adds
  `events`, `source`, `partial_results` and `agent_errors`. Partials are keyed on
  **status**, not on `result is None`: a run can finish and legitimately return
  `None`, and a running run has no result yet, so neither lost anything and
  reporting partials for them would mislead the reader and resend every payload on
  every poll.
- `to_store_json()` / `from_store_json()` is the **durable** round-trip of the
  complete run. `from_store_json` demotes a stored `running` run to `failed` with
  `"interrupted: gateway restarted while running"`, because it can never resume in
  a new process and would otherwise wedge the registry as a zombie that eviction
  refuses to reclaim.

`mark_terminal` is idempotent: only the first terminal transition counts. It
flushes to the store and fires `on_done` (result-to-chat injection).
`record_event` fans out to `on_event` (live WS push) and durably checkpoints every
5th event. `record_agent_result` lands each settled call in memory but deliberately
does **not** force its own store write: `store.save` re-serializes and re-redacts
the entire record synchronously on the gateway event loop, so writing per agent
call would add an O(N) stall per call over a record that grows with every payload.
Durability rides the existing cadence (every 5 events, and each agent call emits
two) plus the guaranteed flush at `mark_terminal`. The trade is explicit: a hard
gateway kill can lose the newest payloads no flush has covered yet, exactly as it
can already lose the newest events. A raising `on_event` / `on_done` subscriber is
swallowed, because a bad subscriber must not break a run.

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
- **Best-effort throughout:** every method swallows failures and logs at debug.
  The in-memory registry is authoritative; a storage failure must never break a
  run. `load_all` skips corrupt files and returns records oldest-file-first by
  mtime.

`RunRegistry.load_persisted()` rehydrates on startup, fills only ids not already
in memory, and re-runs eviction so a store with more records than `max_runs`
cannot leave the registry over its bound.

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
mismatched index would replay the wrong result into the wrong step.

## Gateway wiring

`service.WorkflowService` is the single façade the gateway constructs once at
startup (`DashboardState.workflow_service`); handlers reach it via request app
state, like `state.subagents` / `state.sessions`. It owns one `RunRegistry` (with a
`WorkflowRunStore` unless `persist=False`) and builds a fresh `WorkflowRunner` per
run.

Entry points: `author`, `start`, `start_from_intent`, `status`, `result`,
`list_runs`, `cancel`, `rerun_subtree`, plus the `timeout_secs` property.

Run ids are `wf_NNNNNN` from a per-process monotonic counter, deliberately with no
time or random component so they stay resume-stable. On startup, after rehydrating
persisted runs, the counter continues past the highest persisted sequence so new
ids cannot collide with restored ones.

### Authoring

`author(intent)` turns a natural-language intent into a validated script using the
same in-session model plumbing as the rest of Kiro Crew, looping up to
`_AUTHOR_RETRIES + 1` = 3 attempts and feeding the validation errors back on each
retry. `_strip_fence` peels only the opening fence line and a trailing fence, never
splitting on every ``` , because a literal triple backtick inside the script body
would otherwise truncate it mid-statement.

Authoring runs in a **fresh, isolated, ephemeral** session (`wf-author:<id>`), torn
down with `cleanup=True` the instant it finishes, so a workflow's authoring context
never pollutes (or is polluted by) chat, consolidation, or another run. It uses the
tool-less `kirocrew-lite` agent with `ToolApprovalPolicy.REJECT_ALL`: the dominant
cold-start cost is loading the full MCP toolset and system prompt, and authoring is
pure text generation, so lite is what makes a fresh session cheap. `REJECT_ALL` is
belt-and-suspenders against an alternate ACP backend injecting tools without
`set_mode`.

The authoring system prompt (`service._AUTHOR_SYSTEM`) is the model-facing
statement of this contract: the required module shape, the sandbox rules, the
async-vs-sync split, the "results can be `None`, bind and guard" rule, the exact
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

Pool init failure is caught and falls back to `build_agent_fn`, so pooling can
never break a run start. The runner's `on_complete` hook fires on every exit path
(success, failure, cancellation) to shut the pool down, so warm sessions are always
released.

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

| Route | Body / params | Response |
|-------|---------------|----------|
| `POST /api/workflows/author` | `{intent}` | `{ok, source, meta}` or `{ok:false, errors}` |
| `POST /api/workflows/run` | `{source, args?, name?, budget_total?, timeout_secs?}` | `{run_id}` or `{error}` (400) |
| `POST /api/workflows/run_intent` | `{intent, args?, name?, budget_total?, timeout_secs?}` | `{run_id}` immediately |
| `GET /api/workflows/runs` | | `{runs: [...]}` compact, newest first |
| `GET /api/workflows/runs/{run_id}` | | full snapshot incl. `events` (404 if absent) |
| `POST /api/workflows/runs/{run_id}/cancel` | | `{run_id, cancelled}` |
| `POST /api/workflows/runs/{run_id}/rerun` | `{from_index?, source?}` | `{run_id, from, replayed_before, edited}`; 400 on an invalid edited script, 404 on an unknown run |

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

### MCP tools

`mcp_core.py` exposes seven tools that forward to the routes above:
`workflow_author`, `workflow_run` (takes `source` **or** `intent`, plus `name`,
`args`, `budget_total`), `workflow_status`, `workflow_result`, `workflow_list`,
`workflow_cancel`, `workflow_rerun_subtree`. All share one exit path that redacts
LLM-derived strings.

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
`tests/workflows/malicious/` corpus, and never relax the layering without
`test_workflows_architecture.py`.

> Open question: several module docstrings and test docstrings cite a gate catalog
> at `docs/system-specs/modules/workflow-gates.md` (and short-form "GATES.md")
> using labels like A4, B5, C1, D1, F1, F2, G1. That file does not exist in this
> repo, so the labels are unresolvable. Each gate's actual enforcement is the named
> test, and this spec states the invariants directly rather than by label. Either
> write the catalog or drop the label references from the code comments.

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
