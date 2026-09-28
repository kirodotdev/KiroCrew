# Module Execution and Code Simplification

This reference belongs to `kirocrew-codebase-refactor` and applies only to the
Kiro Crew source repository or one of its worktrees. Root `AGENTS.md` and the
owning Kiro Crew specs outrank this general execution guidance.

Read this reference before editing a refactor scope. It applies to local simplification passes and large-file/class decomposition.

## Build the behavior map

Before changing code, answer:

- What responsibilities does this scope own?
- Which imports, entry points, symbols, signatures, module attributes, CLI commands, files, events, schemas, and side effects are observable?
- Which callers are dynamic, reflected, plugin-provided, or external to the repository?
- Which tests patch private names or assert object identity?
- Which global state, caches, registries, locks, processes, signals, threads, async tasks, or platform APIs are involved?
- What error types, messages, retry policies, logging, and ordering do consumers rely on?
- Why does the current structure exist? Inspect history and nearby conventions when the reason is unclear.

If the behavior authority remains unknown, add characterization evidence or narrow the scope. Deleting untested code because no in-tree caller is visible is unsafe when plugins, config, reflection, or external consumers can reach it.

## Use three simplification lenses

### Reuse

- Prefer an established local helper when it owns the same policy.
- Unify repeated knowledge, not merely repeated syntax.
- Eliminate parallel implementations after all callers converge.
- Reject a shared abstraction when callers have different defaults, errors, permissions, ordering, or likely evolution.

### Quality

- Clarify names and responsibility boundaries.
- Flatten accidental nesting with guard clauses when evaluation and side-effect order remain equivalent.
- Replace long routing ladders with data-driven dispatch only when dispatch order, fallbacks, and errors remain explicit.
- Extract cohesive concepts; do not create one-file-per-function sprawl.
- Prefer direct data flow over hidden mutable state.
- Remove wrappers that add no contract, policy, instrumentation, or useful name.
- Keep abstractions that express a stable concept even if inlining would reduce LOC.

### Efficiency

- Remove redundant traversals, parsing, lookups, allocations, awaits, subprocesses, network calls, and N+1 work when equivalence is clear.
- Preserve laziness, short-circuiting, streaming, cancellation, backpressure, batching, caching, lock order, and exception timing.
- Benchmark performance-sensitive simplifications; do not optimize theoretical costs at the expense of clarity.

When lenses conflict, prioritize behavior, project conventions, and comprehension over maximum DRYness or micro-optimization.

## Decompose large files and classes by ownership

1. Identify state owners and invariants before moving methods.
2. Separate pure calculations and stable protocol types first.
3. Extract one cohesive responsibility at a time: lifecycle, persistence, transport, policy, rendering, platform adapter, command routing, etc.
4. Make dependency direction explicit. A facade may depend on extracted owners; extracted owners should not reach back into the facade except through a narrow interface.
5. Move the canonical implementation once. Re-export or delegate from the old surface only for compatibility.
6. Prevent in-tree code from depending on temporary compatibility pointers when a canonical new owner exists.
7. Remove obsolete state, branches, imports, and duplicate tests only after reachability and behavior are proven.

Do not judge success by the facade alone. Record the largest extracted module, cross-module call count, cycles, import fan-out, and remaining hotspots.

## Compatibility techniques

Choose only what the contract requires:

- stable facade module/class/function;
- re-export of moved public symbols;
- late-bound forwarding where monkeypatch seams require it;
- explicit dependency injection for stateful collaborators;
- compatibility manifest mapping old symbol to new owner;
- identity tests such as `old.Symbol is new.Symbol`;
- byte/structure parity for schemas, CLI help, prompts, config defaults, SQL, wire formats, and localized strings;
- a time-bounded deprecation bridge for external consumers.

Compatibility layers need an exit policy. They must not become a permanent second architecture by accident.

## Comment and documentation simplification

Remove:

- comments that narrate a visible assignment, loop, condition, or signature;
- stale claims contradicted by code;
- commented-out implementation already recoverable from version control;
- repetitive parameter/return prose with no additional contract;
- speculative TODOs with no owner or issue when repository policy permits cleanup.

Keep or improve:

- security and permission rationale;
- compatibility and protocol constraints;
- units, ranges, and wire meanings;
- concurrency, process, signal, FFI, and platform hazards;
- non-obvious performance decisions;
- legal/license notices;
- tool suppressions with a reason;
- “why,” especially when a tempting simplification would be incorrect.

Update developer documentation that refers to removed files, owners, or routing patterns. Do not change user-facing documentation if behavior did not change, except to correct structural developer guidance.

## Dead-code removal standard

Before deletion, inspect:

- static callers and imports;
- dynamic registration, configuration, reflection, serialization, entry points, and plugin discovery;
- packaging/export manifests;
- tests and fixtures;
- history and deprecation policy;
- external compatibility evidence when available.

State the reachability evidence. If external use cannot be ruled out, retain a compatibility path or ask for a product/API decision.

## Avoid semantic traps

Common “simplifications” that may change behavior:

- removing `async` or `await`, which can change when exceptions are thrown and stack/cancellation behavior;
- replacing `||` with `??` or vice versa;
- changing iteration order, container type, equality, hashing, or output representation;
- consolidating exception handling and broadening/narrowing what is suppressed;
- changing eager imports to lazy imports or the reverse;
- replacing sequential work with concurrency;
- moving I/O across a transaction, lock, retry, or authorization boundary;
- evaluating a condition once instead of on every access;
- converting a branch ladder to a dispatch table while losing priority or fallthrough;
- collapsing platform-specific paths into a “universal” helper;
- altering default arguments, environment precedence, or configuration merge order.

Prove equivalence for the actual language and runtime; do not rely on a generic style example.

## Incremental implementation loop

For each coherent extraction or simplification:

1. Name the invariant being preserved.
2. Make the smallest change that establishes the new owner.
3. Inspect the diff immediately.
4. Run the fastest relevant contract test.
5. Remove the replaced path or wire the compatibility facade.
6. Run the affected suite and static gates.
7. Create a recoverable checkpoint when authorized.

Use codemods or AST-aware transforms for broad mechanical changes when the transform can be specified and verified. Sample transformed sites and run semantic gates; automation reduces typing errors, not semantic risk.

## Net-improvement review

Before declaring target-green, compare before and after:

- Is the canonical owner easier to locate?
- Are responsibilities and dependencies clearer?
- Did duplication and long control flow actually decrease?
- Did module count, import edges, startup/import time, indirection, or facade complexity grow? If so, is the trade justified?
- Are new modules cohesive and named by domain responsibility?
- Can a new maintainer understand the behavior faster?
- Is the diff limited to the declared scope?

Revert or redesign a simplification that wins only on line count.
