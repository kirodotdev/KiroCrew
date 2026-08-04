# Dynamic-workflow conformance gates

A *gate* is one named invariant of the dynamic-workflow engine that a test
enforces as a failing build rather than as prose. The engine's docstrings cite
these gates by bare id (`GATE F2`, `GATES A3, A5`, `C3 schema-violating object
rejected`), so the ids need a definition a reader can look up: this file is that
lookup table, and every row is derived from the test that pins it.

The contract the gates protect (the frozen `ctx` surface, the ports, the run
event stream) is specified in [workflows.md](workflows.md). This file only
catalogs the gates.

## How to read a row

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

## Group A: execution semantics

| Gate | Guarantees | Pinned by | Constrains |
|---|---|---|---|
| A3 | `pipeline()` has NO barrier between stages (an item may reach a later stage while another item is still in an earlier one), while `parallel()` IS a barrier (it awaits every thunk before returning). | `test_workflows_dsl.py::test_pipeline_no_inter_stage_barrier_deadlock_proof`, `::test_parallel_is_a_barrier` | `dsl.py` |
| A4 | `Budget` is a HARD ceiling: `charge()` that reaches or passes `total` raises `BudgetExceeded`; `total=None` means unbounded and `remaining()` is `inf`. | `test_workflows_context.py::test_budget_hard_ceiling_raises_at_total`, `::test_budget_hard_ceiling_raises_over_total`, `::test_budget_unbounded_when_total_none` | `context.py` (`Budget`) |
| A5 | A failing thunk or pipeline stage resolves to `None`; the combinator itself never raises, so callers filter `None` instead of handling exceptions. | `test_workflows_dsl.py::test_parallel_failed_thunk_becomes_none_and_does_not_raise`, `::test_pipeline_failed_stage_drops_item_to_none` | `dsl.py` |
| A7 | A run emits exactly the documented event vocabulary, in order, with contiguous `seq` from zero: `run_started` first, one of `run_finished` / `run_failed` / `run_cancelled` last. `REQUIRED_DATA_KEYS` covers exactly `EVENT_TYPES`, and the validator rejects an unknown type or a missing required key. | `test_workflows_events.py::test_required_keys_cover_exactly_event_types`, `::test_every_event_type_has_a_builder_and_validates`, `::test_seq_is_monotonic_from_zero`, `::test_validate_rejects_unknown_type`, `::test_validate_rejects_missing_required_keys`; `test_workflows_runner.py::test_happy_path_emits_full_stream_in_order`, `::test_run_started_first_and_finished_last` | `events.py`, `runner.py`, `__init__.py` (`EVENT_TYPES`) |

## Group B: sandbox, ceilings, and audit

Group B splits into a **static half** (`validate.py` refuses the construct before
the script is ever compiled) and a **runtime half** (`context.build_safe_globals`
makes the capability absent from the exec namespace, so a construct that somehow
slipped past static validation still cannot reach anything). B3 has both halves
by name; the two together are why a single missed AST pattern is not an escape.

| Gate | Guarantees | Pinned by | Constrains |
|---|---|---|---|
| B1 | A workflow script may not `import` anything, and may not reference `eval` / `exec` / `compile` / `open` / `__import__` / `globals` / `locals` / `getattr` / `setattr` / `vars` / `input` / `__builtins__`. The rejection message names the offending symbol. | `test_workflows_invariants.py::test_b1_imports_rejected`, `::test_b1_forbidden_builtins_rejected` | `validate.py` |
| B2 | No dunder attribute or name access (`().__class__`, `__builtins__`, and the rest), and an inline adversarial-escape corpus is rejected wholesale. | `test_workflows_invariants.py::test_b2_dunder_attribute_rejected`, `::test_b2_adversarial_escapes_rejected` | `validate.py` |
| B3 | The nondeterminism modules (`time`, `random`, `uuid`) are unreachable, statically (they cannot be imported) and at run time (no `__import__` in the sandbox namespace, and `SAFE_BUILTINS` excludes nondeterministic and I/O builtins). Determinism is what makes a run stream resume-stable. | static: `test_workflows_invariants.py::test_b3_determinism_modules_rejected`, `::test_b3_safe_builtins_exclude_nondeterminism_and_io`; runtime: `test_workflows_context.py::test_import_statement_fails_in_safe_globals`, `::test_hostile_snippet_fails_at_runtime_in_safe_globals` | `validate.py`, `context.py` (`build_safe_globals`) |
| B4 | Event persistence is JSON only: `serialize_events` / `deserialize_events` round-trip through `json`, reject non-JSON and non-array input, and the module imports no `pickle` / `marshal` / `shelve`. | `test_workflows_events.py::test_round_trip_through_json`, `::test_serialize_output_is_pure_json`, `::test_deserialize_rejects_non_json`, `::test_events_module_has_no_pickle_import` | `events.py` |
| B5 | A wall-clock timeout terminates a runaway run and reports it as a clean `run_failed` with `where == "ceiling"` and `error == "timeout"`. The guard must never let an `asyncio.CancelledError` escape `run()` to the caller. | `test_workflows_runner.py::test_wall_clock_timeout_kills_runaway`, `::test_timeout_never_leaks_cancellederror` | `runner.py` |
| B6 | `AgentCounter` caps lifetime `ctx.agent()` calls per run (default `DEFAULT_MAX_AGENTS_PER_RUN = 1000`) and the cap is enforced through the runner: an unbounded agent loop stops at the limit and ends as `run_failed` at the ceiling. | `test_workflows_context.py::test_agent_counter_raises_past_limit`, `::test_agent_counter_default_limit`; `test_workflows_runner.py::test_agent_count_cap_enforced` | `context.py` (`AgentCounter`), `runner.py` |
| B7 | The exec namespace exposes only `SAFE_BUILTINS` plus `ctx`, so a script has no filesystem or egress reach: `open`, `eval`, `exec`, `compile`, `__import__`, `input` and `getattr` are absent, and benign safe builtins still work. | `test_workflows_context.py::test_safe_globals_only_exposes_ctx_and_safe_builtins`, `::test_hostile_snippet_fails_at_runtime_in_safe_globals`, `::test_safe_builtins_actually_usable` | `context.py` (`build_safe_globals`) |
| B9 | Every hostile script in the on-disk escape corpus (`tests/workflows/malicious/*.py`) is statically rejected, with a non-empty error. The corpus directory must exist and be non-empty, and a new escape idea is added by dropping a file in it: the test parametrizes over the directory. | `test_workflows_malicious.py::test_every_malicious_script_is_rejected`, `::test_corpus_dir_exists_and_is_populated` | `validate.py`, `tests/workflows/malicious/` |
| B10 | Every run writes an audit trail (author, runner, arg KEYS only, one record per agent call with its outcome, and a result *hash*, never the raw result). The sink is injectable and a sink that raises can never fail the run. | `test_workflows_audit.py::test_run_emits_started_and_finished_audit_with_author`, `::test_each_agent_call_is_audited`, `::test_result_hash_never_leaks_raw_result`, `::test_failed_agent_audited_as_failed`, `::test_audit_failure_never_breaks_run` | `runner.py` (`_default_audit`, `_result_hash`), [sel.md](sel.md) |

## Group C: structured output for `ctx.agent(schema=)`

The runtime ships no `jsonschema`, so `schema.py` is a dependency-free validator
for the JSON-Schema subset the DSL uses. C1 to C3 are the three outcomes that
subset has to get right.

| Gate | Guarantees | Pinned by | Constrains |
|---|---|---|---|
| C1 | A conforming object validates clean (empty error list) and is returned to the script. | `test_workflows_schema.py::test_c1_valid_object_passes` | `schema.py` (`validate_against_schema`) |
| C2 | Malformed or invalid model output triggers a *bounded* retry (`DEFAULT_SCHEMA_RETRIES = 2`, so at most initial plus 2 attempts), then returns `None` rather than raising or looping. | `test_workflows_schema.py::test_c2_retry_then_success`, `::test_c2_all_malformed_returns_none`, `::test_c2_schema_violation_retried_then_none` | `schema.py` (`run_with_schema`) |
| C3 | An object that parses as JSON but violates the schema is rejected, not returned: missing `required` key, wrong type, `enum` violation, and `bool` not counting as `integer`. | `test_workflows_schema.py::test_c3_missing_required_rejected`, `::test_c3_wrong_type_rejected`, `::test_c3_enum_violation_rejected`, `::test_c3_bool_is_not_integer` | `schema.py` |

## Group D: Kiro Crew's own `ctx` primitives

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

## Group E: dashboard tab (unpinned)

| Gate | Guarantees | Pinned by | Constrains |
|---|---|---|---|
| E1 | Undocumented. The only statement of intent is an inline comment in `website/src/apps/workflows/WorkflowsPage.tsx` ("invalid script blocks the run") on the run handler, which validates before it runs. No test asserts it by id. | Nearest coverage: `website/src/test/WorkflowsPage.test.ts` (pure event-stream folding), `website/playwright/builtin-apps.spec.ts` (`/workflows` renders). | `website/src/apps/workflows/WorkflowsPage.tsx` |
| E2, E3, E4 | Undocumented. These ids appear only inside the range `E1-E4`, described as Playwright gates against a dev instance; no individual id is defined or asserted anywhere in the tree. | See `website/src/test/WorkflowsPage.test.ts` (which names the range) and `website/playwright/builtin-apps.spec.ts`. | `website/src/apps/workflows/` |

The backend half of the tab is covered without gate ids, in
`test_workflows_app.py` (manifest shape, `handle_validate` / `handle_run` /
`handle_examples`, and redaction of credentials and exfiltration URLs before a
run payload leaves the handler).

## Group F: fitness gates on the engine itself

Group F gates are enforced as pure-stdlib AST scans with no `import-linter` or
`git` dependency, so they hold in any checkout: git state is environment-fragile
and a git-dependent gate can redden a clean trunk.

| Gate | Guarantees | Pinned by | Constrains |
|---|---|---|---|
| F1 | The layering holds: `validate` / `dsl` / `schema` / `events` / `registry` / `__init__` and the optional adapters (`agent_exec`, `agent_pool`, `store`) are leaves with no intra-package siblings; `context` may import `validate`; `runner` may import `validate`, `dsl`, `events`, `context`, `schema`, `registry`; `service` sits above the runner. No module may import backwards, no module escapes the declared contract, and the engine must not reach into `kiro_crew.dashboard.state` or `kiro_crew.dashboard.ws` (progress goes through the event bus or a port). | `test_workflows_architecture.py::test_layering_no_backward_or_unexpected_sibling_imports`, `::test_engine_does_not_import_dashboard_internals`, `::test_every_module_is_covered_by_the_contract` | all of `src/kiro_crew/workflows/` |
| F2 | The frozen contract in `workflows/__init__.py` cannot drift silently: `__all__`, the `WorkflowContext` data attributes, its exact method set, each method's signature and async-ness, each port's methods and `runtime_checkable`-ness, `EVENT_TYPES` (exact and ordered), and the event envelope keys plus JSON round-trip. Changing any of them is an explicit re-freeze that must update `__init__.py`, [workflows.md](workflows.md), and this test together. | `test_workflows_conformance.py::test_all_exports_exact`, `::test_ctx_method_set_exact`, `::test_ctx_method_signature`, `::test_port_method_signature`, `::test_event_types_exact_and_ordered`, `::test_event_envelope_keys_exact` | `__init__.py` |
| F3 | Every implementation module under `workflows/` is imported by at least one `test/test_workflows_*.py`, so a module cannot be added without a test that reaches it. The gate carries its own negative control, so it cannot silently stop catching orphans. | `test_workflows_presence.py::test_every_workflows_module_has_a_referencing_test`, `::test_presence_gate_flags_an_orphan_module` | all of `src/kiro_crew/workflows/` |

## Group G: authoring reliability

The DSL is only useful if an agent can author it on the first try, so authoring
reliability is a measured gate rather than an assumption. The candidate set mixes
the shipped examples (known-good output) with intentionally plausible-but-flawed
scripts, so the rate is a measurement and not a tautology.

| Gate | Guarantees | Pinned by | Constrains |
|---|---|---|---|
| G1 | The first-try valid-script rate over the candidate set is at or above `G1_TARGET = 0.80`, every shipped example validates, and each deliberately flawed candidate is actually rejected. | `test_workflows_authoring_eval.py::test_g1_shipped_examples_all_validate`, `::test_g1_first_try_valid_rate_meets_target`, `::test_g1_flawed_candidates_are_caught` | `validate.py`, the shipped example scripts the test resolves |
| G2 | Every script that validates terminates cleanly against a stub provider: `run_started` first, `run_finished` or `run_failed` last, `seq` contiguous. It never hangs, and no exception escapes the runner (a run that ends `run_failed` still satisfies G2, which is about termination and stream shape). | `test_workflows_authoring_eval.py::test_g2_valid_scripts_run_to_completion`, `::test_g2_simple_authored_script_fully_succeeds` | `runner.py`, `validate.py` |

## Adding or changing a gate

1. Write the test first: a gate is the test, and this table is its index.
2. Cite the gate by id in the source docstring it constrains, and add the row
   here in the same change.
3. Never relax a check to make a red gate green. A group-B case that flips from
   RED to GREEN because a validator check was loosened is a sandbox regression,
   not a fix; a red F3 means the new module needs a test, not an exemption.
