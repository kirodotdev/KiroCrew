# Quality Engineering Crew

Status: approved design; implementation not started.

## Why

Kiro Crew already has native `software-delivery` and `knowledge-quality` packages. The work performed across recent sessions also contains a repeated, independent quality workflow: test planning, browser and simulator validation, E2E evidence capture, accessibility checks, UX review, and release-readiness classification.

That work should be represented by a dedicated read-only Crew rather than by adding more responsibilities to Software Delivery. A separate package keeps implementation and validation independent, gives automatic routing an explicit destination, and makes blocked evidence visible instead of allowing an implementation workflow to self-certify.

## Goals

- Add a native `quality-engineering` Crew package parallel to the existing Crew packages.
- Provide three read-only roles: QA Strategist, E2E Engineer, and UX Reviewer.
- Route high-confidence QA, E2E, UX, and release-readiness requests automatically.
- Support deterministic direct invocation through `/crew quality-engineering <request>`.
- Run real E2E checks through a bounded runner in an isolated environment.
- Produce schema-validated, bounded reports with evidence, findings, gaps, and next actions.
- Fail closed on missing project bindings, unavailable capabilities, invalid handoffs, timeouts, and incomplete evidence.
- Preserve the existing default-agent path for ordinary and ambiguous chat.

## Non-goals

- Do not expose internal worker names as public agent aliases.
- Do not grant source writes, configuration writes, commits, or pushes to any Quality Engineering role.
- Do not replace or refactor the existing Crew package contract into a generic factory.
- Do not make the runner accept arbitrary shell commands from user text or model output.
- Do not add a new always-on background worker.
- Do not change the existing Software Delivery or Knowledge Quality route semantics except for additive registry and routing integration.
- Do not treat a design proposal as a release or modify the live Gateway.

## Existing constraints

The implementation follows the current native Crew boundaries:

- `CrewCatalog` validates versioned role, route, handoff, and policy records.
- `role_resolver` validates schemas and executes roles through the existing workflow `ctx.agent()` path.
- `crew_dispatch` maps private workflow names to native Crew package runtimes.
- Internal role agents are namespaced and hidden from public alias lookup.
- The dashboard's automatic routing requires high confidence and an authoritative absolute project path.
- The dashboard serves user-facing slash-command descriptions from one backend catalog and a frontend fallback.
- Kiro Crew changes are developed and validated inside the dedicated feature worktree, never against the live Gateway.

## Package boundary

The new package lives at:

```text
src/kiro_crew/crews/quality_engineering/
├── __init__.py
├── package.py
├── schemas.py
├── catalog.json
├── agent_specs/
│   ├── qa-strategist.json
│   ├── e2e-engineer.json
│   └── ux-reviewer.json
└── prompts/
    ├── kirocrew-quality-engineering-qa.txt
    ├── kirocrew-quality-engineering-e2e.txt
    └── kirocrew-quality-engineering-ux.txt
```

`package.py` owns resource loading, catalog validation, materialization, route execution, bounded result construction, and aggregation. It follows the same package-owned resource and collision-safe materialization pattern as the existing native Crew packages.

The package owns the Crew id `quality-engineering` and the private workflow name `__kirocrew.crew.quality-engineering`. The public command accepts only the Crew id; it never accepts a worker agent name.

## Roles and permissions

### `qa-strategist`

Mission: turn a request and available project metadata into a bounded QA plan with acceptance criteria, a test matrix, regression risks, and non-goals.

Capabilities: project read, code intelligence, search, and report. It has no source-write or shell execution capability.

Handoff: `qa_plan`.

### `e2e-engineer`

Mission: interpret results from the bounded evidence runner, classify E2E checks, identify failures and environment gaps, and produce a structured E2E report.

Capabilities: structured evidence read and report. It does not receive raw shell execution or source-write capability. The native Crew runtime invokes the bounded runner before this role is executed.

Handoff: `e2e_report`.

### `ux-reviewer`

Mission: inspect the project surface and collected evidence for usability, accessibility, interaction consistency, visual regressions, and evidence gaps.

Capabilities: project/evidence read, code intelligence, search, and report. It has no source-write or shell execution capability.

Handoff: `ux_report`.

All three roles declare `side_effects: "none"`. The Crew policy is `target_write: "none"` and `push: "none"`. Agent specs use `includeMcpJson: false` and do not pre-authorize a write-capable tool.

## Routes

The catalog declares these routes:

| Route | Roles | Purpose |
|---|---|---|
| `qa_plan` | QA Strategist | Plan validation without running checks. |
| `e2e_validation` | QA Strategist → E2E Engineer | Run bounded checks and classify E2E evidence. |
| `ux_review` | QA Strategist → UX Reviewer | Review the product surface and available evidence. |
| `full_quality_review` | QA Strategist → E2E Engineer → UX Reviewer | Produce a complete quality report. |

Direct invocation defaults to `full_quality_review`. Automatic routing selects a narrower route only when the message contains a high-confidence domain signal.

## Entry points and routing

### Automatic routing

`classify_message()` gains an additive Quality Engineering branch. It must preserve the current precedence and fallback rules:

- QA, test plan, regression, acceptance criteria, and coverage language maps to `qa_plan`.
- E2E, Playwright, browser flow, simulator, and end-to-end language maps to `e2e_validation`.
- UX, accessibility, usability, interaction, visual, and design-review language maps to `ux_review`.
- Release readiness, full validation, and complete verification language maps to `full_quality_review`.
- A message with conflicting or insufficient markers remains low confidence and falls back to the existing clarification/default path.
- Ordinary questions, ordinary searches, and messages without a project binding are not silently routed to Quality Engineering.

All automatic routes use the existing fixed dynamic workflow source and the additive internal workflow dispatch path.

### Direct invocation

The dashboard recognizes:

```text
/crew quality-engineering <request>
```

The command is parsed locally before provider session acquisition. It validates the exact Crew id, requires a non-empty request, preserves the slot's active agent, and starts the same native workflow used by automatic routing with `full_quality_review`.

The command does not expose internal role aliases, does not change the slot into Crew Mode, and uses the existing busy-slot queue/hold behavior when another turn or automatic Crew run is active. The command is added to the dashboard slash-command catalog so autocomplete and the backend agree on its availability.

## Data flow

```text
ordinary message ──┐
                   ├─ route decision ──┐
/crew command ─────┘                   │
                                       ▼
                 __kirocrew.crew.quality-engineering
                                       │
                       QualityEngineeringCrew.run()
                                       │
                QA Strategist → bounded runner → E2E Engineer
                                       │
                              UX Reviewer
                                       │
                              quality_report
```

Both entry points converge after command/routing validation. No second orchestration engine is introduced.

The workflow validates the project path, creates a run-scoped evidence location, runs the selected bounded checks where the route requires them, invokes roles through `execute_role()`, validates each role output against its schema, and aggregates the final report. The workflow emits normal phases and logs so the dashboard can show progress through the existing workflow event stream.

## Handoff contracts

### `qa_plan`

Required fields:

- `scope`
- `acceptance_criteria`
- `test_matrix`
- `regression_risks`
- `non_goals`

### `e2e_report`

Required fields:

- `status`: `passed`, `failed`, or `blocked`
- `checks`
- `failures`
- `evidence`
- `environment`
- `evidence_gaps`

### `ux_report`

Required fields:

- `status`: `passed`, `failed`, or `blocked`
- `usability_findings`
- `accessibility_findings`
- `interaction_findings`
- `evidence`
- `evidence_gaps`

### `quality_report`

Required fields:

- `status`: `passed`, `failed`, or `blocked`
- `checks`
- `findings`
- `evidence`
- `evidence_gaps`
- `next_actions`
- `role_reports`

Arrays and text fields are bounded. Results are redacted and truncated before persistence or delivery, and raw prompts, credentials, and unbounded command output are never embedded in the final report.

## Bounded E2E runner

The native package owns a `QualityEvidenceRunner` boundary. It accepts structured input only:

```text
project_path
adapter
check_ids
evidence_dir
timeout
```

It never accepts an arbitrary command string from user text or model output. Adapter and check identifiers come from an explicit registry. The runner uses validated argv with `shell=False`, confines the working directory to the disposable run workspace, caps runtime and output size, tracks the process tree, and copies only bounded redacted evidence into the run evidence directory.

The runner executes checks in a disposable isolated environment. Depending on host and project capabilities this may be a temporary worktree, an isolated Kiro Crew pod, a browser session with isolated state, or an iOS simulator test environment. Unsupported browser, simulator, dependency, or project capabilities return `blocked` rather than falling back to arbitrary shell execution.

Evidence may include bounded stdout/stderr, structured check results, screenshots, and other approved media. Evidence paths are run-scoped and are not allowed to escape the evidence root. Temporary workspaces are cleaned up after completion or cancellation.

## Safety and failure behavior

| Condition | Required result |
|---|---|
| Unknown Crew or route | Stable command error; no workflow started. |
| Missing or relative project binding | `blocked` with a project-path reason. |
| Sensitive or disallowed path | Refuse before runner creation. |
| Unsupported adapter/capability | `blocked`; no arbitrary fallback. |
| Schema-invalid role output | Use the existing bounded schema retry, then `blocked` if invalid. |
| Role timeout or missing result | Preserve valid partial handoffs and aggregate `blocked`. |
| E2E or UX failure | The full review cannot be `passed`. |
| Important evidence gap | Report `blocked` unless the route explicitly treats the check as optional. |
| Evidence redaction failure | Withhold the evidence and report the gap. |
| Workflow cancellation or resume | Use existing workflow state and run-scoped evidence identity; do not duplicate completed evidence unnecessarily. |

A successful response means the checks and evidence contract completed, not merely that the workflow process returned without an exception.

## Error surface

Backend-owned non-2xx responses carry machine-readable codes. The command and workflow paths use bounded codes such as:

- `crew.command.invalid`
- `crew.quality_engineering.unknown`
- `crew.quality_engineering.request_required`
- `crew.quality_engineering.project_required`
- `crew.quality_engineering.path_blocked`
- `crew.quality_engineering.route_unknown`
- `crew.quality_engineering.adapter_unavailable`
- `crew.quality_engineering.evidence_blocked`
- `crew.quality_engineering.role_blocked`
- `crew.quality_engineering.timeout`

User-facing text goes through the existing localization path; internal details remain in bounded audit/log fields.

## Testing and validation

### Contract tests

- Load and validate the catalog, role references, routes, policies, and schemas.
- Verify each agent spec has the expected names, model policy, MCP policy, and no write capability.
- Verify package resource loading, collision-safe materialization, and idempotent installation.
- Verify all handoff schemas reject missing, malformed, over-sized, or invalid status fields.

### Routing and command tests

- Route high-confidence QA, E2E, UX, and release-readiness messages to the expected Crew and route.
- Preserve default behavior for ordinary, ambiguous, and unbound messages.
- Parse `/crew quality-engineering <request>` locally, reject unknown Crew ids and empty requests, and preserve active-agent/queue semantics.
- Verify automatic and direct entry points converge on the same internal workflow.

### Runner and safety tests

- Reject arbitrary command strings, shell interpolation, path traversal, sensitive paths, and evidence-root escapes.
- Verify argv execution, timeout, process-tree cleanup, output caps, and redaction.
- Verify unsupported adapters and missing browser/simulator capabilities become `blocked`.
- Verify source and config files in the original project remain unchanged after a run.
- Verify cancellation, partial results, and resume do not fabricate a pass or duplicate evidence.

### Build gates

Run the focused package/routing tests first, followed by the repository's required backend and frontend checks for touched surfaces. Because the host may have limited memory, use targeted tests during iteration and run the full gate only after the focused path is stable.

## Compatibility and rollout

The change is additive:

- Existing Software Delivery and Knowledge Quality ids and routes remain unchanged.
- Existing ordinary chat remains on the default agent path unless the new high-confidence markers match.
- Internal worker names remain hidden from public agent aliases.
- The new `/crew` command is only a Crew-level entry point and does not change `/agent` semantics.
- The live Gateway is not modified during development; the feature is validated in the dedicated worktree and becomes available to the current dashboard only after the feature is merged, built, deployed, and restarted.

Implementation begins only after this proposal has been reviewed. The proposal itself does not authorize a commit, push, merge, or live Gateway restart.
