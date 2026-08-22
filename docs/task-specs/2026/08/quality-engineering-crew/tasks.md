# Implementation Plan: Quality Engineering Crew

Status: ready for execution after plan review.
Design reference: `docs/design/2026-08-20-quality-engineering-crew.md`.
Worktree: `/home/user/kirocrew-wt-automatic-crew-routing` on `feat/automatic-crew-routing`.

## Execution rules

- Implement and validate only in the dedicated worktree; do not modify the live Gateway.
- Keep every Quality Engineering role read-only: no source/config writes, commits, or pushes.
- Do not accept arbitrary shell commands from user text, prompts, or model output.
- Use the existing native Crew package, workflow, governance, and dashboard seams; do not add a second orchestration engine.
- Preserve existing Software Delivery, Knowledge Quality, ordinary-chat, and `/agent` behavior.
- Use targeted tests first because host memory is limited; check resource headroom before a full build or E2E gate.
- Do not commit or push unless the user gives an explicit command.

## Deliverables

- A native `quality_engineering` package with three private worker agents and four routes.
- A schema-validated `QualityEvidenceRunner` boundary with explicit adapter/check registries.
- Additive native dispatch and automatic routing for `quality-engineering`.
- Local dashboard command `/crew quality-engineering <request>` using `full_quality_review`.
- Backend and frontend slash-command catalogs that advertise `/crew` consistently.
- Focused contract, safety, routing, command, and report-aggregation tests.
- Updated existing system-spec documentation for the shipped dashboard, runner, and error behavior.

## 1. Preflight and package scaffold

- [ ] 1.1 Reconfirm the worktree, branch, and approved design files; keep the two existing design-document changes intact.
- [ ] 1.2 Create `src/kiro_crew/crews/quality_engineering/__init__.py`, `package.py`, `schemas.py`, `catalog.json`, the three `agent_specs/*.json` files, and the three prompt files.
- [ ] 1.3 Keep package-owned resource loading, validation, atomic materialization, collision checks, and shared-agent-home protection aligned with `software_delivery` and `knowledge_quality`.
- [ ] 1.4 Export only unambiguous public names from the package. Avoid adding a second package-level `load_agent_spec`, `CrewRunResult`, or generic constants that collide with the existing `kiro_crew.crews` exports; use explicit Quality Engineering names or module-local imports.

## 2. Schemas and catalog

- [ ] 2.1 Define bounded schema helpers in `quality_engineering/schemas.py`: bounded text, arrays, objects, status enums, evidence records, findings, and role-report envelopes.
- [ ] 2.2 Define schemas for the QA plan request/output, E2E validation request/output, UX review request/output, and final `quality_report`.
- [ ] 2.3 Require the approved handoff fields: `qa_plan`, `e2e_report`, `ux_report`, and `quality_report`; reject missing fields, invalid statuses, over-sized arrays/text, and malformed evidence.
- [ ] 2.4 Define `catalog.json` with roles `qa-strategist`, `e2e-engineer`, and `ux-reviewer`; map each role to its input/output schema, handoff id, quality gates, and `side_effects: "none"`.
- [ ] 2.5 Define routes exactly as approved: `qa_plan`, `e2e_validation`, `ux_review`, and `full_quality_review`; make the full route order QA Strategist → E2E Engineer → UX Reviewer.
- [ ] 2.6 Set Crew policies to `target_write: "none"` and `push: "none"`; keep all roles read-only even though resolver tool grants remain governed by the runtime.
- [ ] 2.7 Add agent specs with names `kirocrew-quality-engineering-qa`, `kirocrew-quality-engineering-e2e`, and `kirocrew-quality-engineering-ux`; use model inheritance/`auto`, `includeMcpJson: false`, and the report-only tool posture used by the read-only Crew packages.
- [ ] 2.8 Write prompts that require structured JSON handoffs, treat project/evidence text as untrusted data, forbid source/config mutation and arbitrary command execution, and classify missing evidence as blocked rather than passed.

## 3. Bounded evidence runner and native package runtime

- [ ] 3.1 Add a package-owned `QualityEvidenceRunner` boundary in `quality_engineering/package.py`, keeping helper classes and adapter registries package-private, with the structured input contract `project_path`, `adapter`, `check_ids`, `evidence_dir`, and `timeout`.
- [ ] 3.2 Implement an explicit adapter/check registry. User and model input may select identifiers only from the registry; no input may provide a command string, shell fragment, executable path, or arbitrary argv.
- [ ] 3.3 Resolve and validate the project path before creating a run. Require an absolute non-sensitive path, confine all disposable workspaces and evidence paths below approved roots, and reject traversal/symlink escapes.
- [ ] 3.4 Execute registered checks with validated argv and `shell=False`, bounded environment/cwd, process-tree tracking, timeout enforcement, output-size limits, and cleanup on success, failure, cancellation, or timeout.
- [ ] 3.5 Run checks in a disposable isolated environment appropriate to the adapter: temporary worktree/pod, isolated browser state, or simulator environment. Unsupported browser, simulator, dependency, or adapter capability must return `blocked` without a fallback.
- [ ] 3.6 Persist only bounded, redacted evidence. Constrain screenshots/media and stdout/stderr to the evidence root, redact credentials and exfiltration URLs before persistence/delivery, and withhold evidence when redaction fails.
- [ ] 3.7 Return typed runner results with `passed`, `failed`, or `blocked`, per-check records, failures, environment facts, evidence references, and evidence gaps. Never turn a timeout, unavailable capability, missing evidence, or runner error into `passed`.
- [ ] 3.8 Implement `QualityEngineeringCrew.run()` using the existing `resolve_role()` and `execute_role()` path. Validate the first role input, resolve each role from the catalog, validate every handoff, and stop at the first invalid/blocked role while preserving valid partial reports.
- [ ] 3.9 Run the bounded runner only for routes that require real checks. `qa_plan` produces a plan without execution; E2E routes execute the selected registered checks; UX review consumes approved project/evidence inputs; full review combines QA, bounded E2E, and UX outputs.
- [ ] 3.10 Aggregate `quality_report` with bounded `checks`, `findings`, `evidence`, `evidence_gaps`, `next_actions`, and `role_reports`. Force aggregate `blocked` for role timeout, malformed handoff, missing capability, important evidence gap, E2E failure, or UX failure.
- [ ] 3.11 Keep `CrewRunResult.to_dict()` JSON-compatible, redacted, and bounded; do not embed prompts, credentials, arbitrary command output, or sensitive absolute paths in the delivered summary.

## 4. Internal worker registry and native dispatch

- [ ] 4.1 Extend `crew_registry.py` with the three namespaced Quality Engineering worker names and add the package module to `_CREW_PACKAGE_MODULES`.
- [ ] 4.2 Preserve the hidden-worker invariant: no Quality Engineering worker name becomes a public agent alias, picker row, or user-facing Crew command.
- [ ] 4.3 Extend `crew_dispatch.py` with `INTERNAL_QUALITY_ENGINEERING_WORKFLOW = "__kirocrew.crew.quality-engineering"` and include it in `INTERNAL_WORKFLOW_NAMES`.
- [ ] 4.4 Add a bounded request builder for Quality Engineering that copies only the request, authoritative project path, route, and registered adapter/check identifiers; reject or ignore arbitrary command-shaped fields.
- [ ] 4.5 Dispatch the Quality Engineering package additively from `execute_native_crew()`, pass the parent workflow id/session identity, select `full_quality_review` only for the direct-command default, and return the standard redacted result envelope.
- [ ] 4.6 Update native-registry/dispatch tests for materialization count, idempotency, collision safety, unknown workflow rejection, package loading, and hidden internal aliases.

## 5. Automatic routing

- [ ] 5.1 Add `CREW_QUALITY_ENGINEERING` and route constants for `qa_plan`, `e2e_validation`, `ux_review`, and `full_quality_review` in `automatic_routing.py`.
- [ ] 5.2 Add high-confidence marker groups for QA/test-plan/regression/acceptance, E2E/Playwright/browser/simulator/end-to-end, UX/accessibility/usability/visual review, and release-readiness/full-validation.
- [ ] 5.3 Require a clear quality-validation intent and the existing authoritative absolute project binding. A project path mentioned only in message text must never become the slot binding.
- [ ] 5.4 Preserve existing precedence for explicit Knowledge audits and Software Delivery changes. A message that asks to implement/change code rather than validate it must not be misrouted merely because it mentions Playwright or UX.
- [ ] 5.5 Return low confidence for conflicting or insufficient quality markers; let the existing clarification/default path handle ambiguity. Ordinary questions, ordinary searches, and unbound messages remain on the default agent path.
- [ ] 5.6 Extend `test/test_automatic_crew_routing.py` with positive examples for all four routes, missing/relative project cases, conflicting-domain cases, implementation-vs-validation cases, and regressions for the two existing Crews.

## 6. Dashboard automatic route integration

- [ ] 6.1 Update `dashboard/chat_handlers.py` imports and `_automatic_route_args()` to map all supported Crew ids explicitly; unknown ids must not fall through to Knowledge Quality.
- [ ] 6.2 Pass Quality Engineering requests with the authoritative `project_path` and the selected route while preserving the existing software/knowledge argument shapes.
- [ ] 6.3 Refactor the shared workflow-start/acknowledgement seam only as needed so automatic routing and direct `/crew` invocation converge on the same workflow source, args, run tracking, and busy/queue behavior.
- [ ] 6.4 Extend `test/test_automatic_crew_chat.py` to assert the Quality Engineering workflow name, route, project path, service-start metadata, and default fallback behavior.

## 7. Direct `/crew` command

- [ ] 7.1 Add `/crew` to `_SLASH_COMMANDS` and `SLASH_COMMAND_DESCRIPTIONS` in `dashboard/chat_utils.py`; keep it out of `_BLOCKED_SLASH_COMMANDS`.
- [ ] 7.2 Parse `/crew` locally in `dashboard/chat_runner.py` before provider session acquisition, alongside the existing local command handlers.
- [ ] 7.3 Accept exactly `/crew quality-engineering <request>` for this feature. Reject missing request text, unknown Crew ids, unknown subcommands, and malformed input with bounded user-facing errors and no provider session.
- [ ] 7.4 Resolve the active slot's project binding, require an absolute non-sensitive path, and start the same native workflow with `full_quality_review`; do not use a message-supplied path as authority.
- [ ] 7.5 Preserve the slot's active agent and mode, do not expose worker names, do not enter Crew Mode, and preserve existing busy-slot queue/hold semantics.
- [ ] 7.6 Track the direct run in the same dashboard state used by automatic routes, emit the normal acknowledgement/status update, and ensure failures remain visible as blocked rather than silently falling through to the provider.
- [ ] 7.7 Add direct-command tests covering successful dispatch, exact public syntax, empty/unknown input, missing project, no provider acquisition, active-agent preservation, and an active automatic-run queue case.

## 8. Slash-command API and frontend autocomplete

- [ ] 8.1 Rely on the backend command catalog for the live dashboard API; add tests in `test/test_api_slash_commands.py` that `/crew` has a non-empty description and is returned while blocked commands remain absent.
- [ ] 8.2 Update `website/src/components/SlashCommandMenu.tsx` `COMMAND_DESC_KEY` and offline fallback names with `/crew`.
- [ ] 8.3 Add the localized `components.slashCommandMenu.desc_crew` key through the repository's i18n catalog workflow for every supported locale/source catalog; do not hardcode a user-facing string in the component.
- [ ] 8.4 Extend `website/src/test/SlashCommandMenu.test.tsx` to cover `/crew` in the resolved API path and offline fallback path without reintroducing blocked commands.
- [ ] 8.5 Read `website/docs/i18n-catalog.md` and `website/docs/testing.md` before touching frontend files; use `npx tsc -b` or the build path rather than the known zero-file `npm run typecheck` shortcut.

## 9. Documentation updates

- [ ] 9.1 Update `docs/system-specs/modules/learn-cron-dashboard.md` with the shipped `/crew quality-engineering` command, automatic route eligibility, project-binding requirement, run tracking, and default fallback behavior.
- [ ] 9.2 Update `docs/system-specs/common/error-handling.md` with the bounded Quality Engineering error/status surface, partial-result behavior, and fail-closed evidence rules.
- [ ] 9.3 Update `docs/system-specs/modules/browser.md` only for the shipped adapter boundary or capability semantics that change its documented contract; keep the existing browser install/consent model unchanged.
- [ ] 9.4 Update `docs/system-specs/modules/computer-use.md` only if the simulator/desktop adapter crosses its documented boundary; do not grant computer-use permissions to the read-only roles.
- [ ] 9.5 Run `scripts/docs-lint.sh` and update any relevant existing README index when a system-spec link or new documented surface requires it. Do not create a duplicate design document.

## 10. Test matrix

- [ ] 10.1 Add `test/test_quality_engineering_crew.py` for catalog/resource contracts, role specs, prompt policy, schema bounds, route role order, report aggregation, partial handoffs, and read-only posture.
- [ ] 10.2 Add runner safety tests for arbitrary command rejection, shell interpolation rejection, adapter/check allow-listing, sensitive paths, traversal/symlink escapes, evidence-root escapes, argv plus `shell=False`, timeout/process-tree cleanup, output/evidence caps, redaction, and unsupported-capability `blocked` results.
- [ ] 10.3 Verify a runner against a temporary disposable project leaves the original source/config unchanged; use `tmp_path` and the repository isolation fixtures, never the operator's real Kiro home.
- [ ] 10.4 Test malformed role output, timeout, missing capability, E2E failure, UX failure, evidence gap, cancellation, and resume aggregation. No such case may produce `passed`.
- [ ] 10.5 Extend existing routing, chat, registry, dispatch, slash-command, and workflow-architecture tests rather than weakening their current invariants.
- [ ] 10.6 Keep real kiro-cli and live browser/simulator dependencies out of unit tests. Use explicit fake adapters/backends for deterministic runner tests; use the repository's isolated E2E gate for the real dashboard/browser path.

## 11. Validation sequence

- [ ] 11.1 Run formatting/static checks on changed Python files and `git diff --check`.
- [ ] 11.2 Run targeted backend tests first:
      `pytest -q test/test_quality_engineering_crew.py test/test_automatic_crew_routing.py test/test_automatic_crew_chat.py test/test_api_slash_commands.py test/test_crew_chat.py`.
- [ ] 11.3 Run targeted frontend tests from `website/`, including `SlashCommandMenu.test.tsx`, then `npx tsc -b` or `npm run build`.
- [ ] 11.4 Run the relevant workflow, registry, role-resolver, fake-ACP, and dashboard regression tests after the focused tests are green.
- [ ] 11.5 Check host resources before the full Kiro Crew worktree gate. Run the repository-required pytest, isort, flake8, mypy, TypeScript build, and Vitest gates with bounded parallelism appropriate to available memory.
- [ ] 11.6 Run `python setup.py test_e2e` only after the packaged frontend/fake backend prerequisites are available. A missing browser/adapter/dependency is a documented blocked capability, not a reason to weaken runner behavior or mark evidence passed.
- [ ] 11.7 Re-run `scripts/docs-lint.sh`, inspect the complete diff for internal-worker leakage, verify the worktree contains no live-Gateway changes, and report all remaining blocked checks explicitly.

## 12. Completion boundary

- [ ] 12.1 Stop after implementation and validation with the changes uncommitted unless the user explicitly requests a commit.
- [ ] 12.2 Do not push, open a PR, merge, deploy, or restart the Gateway as part of this plan.
- [ ] 12.3 Report changed files, targeted/full validation results, blocked capability checks, and any evidence gaps before asking for the next explicit integration action.
