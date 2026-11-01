# BYO 0.2.3 · Stage 3 — transaction-scoped unified backend linkage

Date: 2026-10-25. Based on the Stage 2 candidate code. **This is a standalone test candidate, not an installed or host-verified upgrade.** Fix 03 and Stage 1/2 original archives remain unchanged.

## What changed

- `GET /tasks/linkage` reports the current Crew host-configured main/member defaults and candidate backends registered as **known plus launchable**, with member targets separately checked against `ACP_BACKENDS_MEMBER_DISPATCH`. A missing Crew registry disables switching.
- `POST /tasks/linkage/preview` accepts `target_main` and `member_mode` (`follow`, `keep`, `fixed` with `fixed_member`) and returns the exact two Crew default values that would change. It shows possible **Agent Cron and background/Subagent model pin conflicts** and explicitly does not claim to know running task backends.
- `POST /tasks/linkage/apply` requires the exact preview ID (calculated over effective defaults plus current Cron job/model facts). It refreshes the plan, rejects stale approvals, detects `config.local.json` overrides, then CAS-checks original values and changes only `agent.acp_backend` / `agent.member_acp_backend` via **one** Crew `update_config_locked()` transaction. Re-reads effective config and, on mismatch, attempts CAS rollback; refuses rollback over a concurrent edit. No job, session, model pin, memory, skill, or permission setting is modified.
- Task UI provides preview/confirm, includes five-locale strings, and retains the original member-only manual sync. This is a **one-time switch**, **not** a persistent watcher or forced override of Crew's member/task-specific backend selection.

## Limits / safety

- A host registry + launch entry is a minimal write precondition, not proof of full Crew capability parity. If a Crew source version changes the registry shape, apply fails closed rather than guessing.
- Cron Agent model conflict detection is **advisory**, not proof of which backend a job will actually run under. Script/Command Cron remain outside ACP linking. Already-running conversations/workflows/Heartbeat/TaskRunner are not migrated or restarted.
- The preview hash protects against stale state between preview and final apply. It does not create a persistent policy and it cannot prevent every external race involving independent Cron edits; the two host ACP defaults themselves are CAS-checked under lock.
- A higher-priority override can still be externally introduced during a write; effective config is re-read and rollback is attempted, but a concurrent modification may make unconditional rollback unsafe. Such a case is surfaced as requiring manual inspection, never silently overwritten.
- No Windows/macOS/Linux **real installed Crew** end-to-end verification has been performed. Crew `v0.10.0-insider.5` exact source contract remains `pending_ci_checkout`; do not install over your only live Crew profile.

## Validation

Tests run in the local Linux environment, synthetic Crew host and real BYO ACP subprocess (no real provider API). Cases include preview does not write, fixed/follow/keep, unregistered/unlaunchable backend, stale preview, `config.local.json` override, role/Cron model pin warnings, single-lock write, rollback on effective override, legacy Crew member handling. Browser UI tests are separate. Cross-platform test job already exists for Linux/macOS/Windows; no remote workflow success is claimed.
