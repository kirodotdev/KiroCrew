# Kiro BYO 0.2.3 — Stage 4 execution-integrity candidate

Date: 2026-10-25. Starting point: independently archived Stage 3 source candidate (`0.2.3+58d99338292e3d83`). This is **a Stage 4 safety-hardening increment, not the completion of all Stage 4 goals and not a platform-verified release**. No user's local KiroCrew environment was touched.

## Scope and observed failures (red before green)

1. `ProcessManager.terminate()` had a branch that marked `state='killed'` even if `proc.poll()` still returned `None` after the attempted force kill. The resource then stopped counting against the concurrent process limit despite being OS-live. Reproduced with a deterministic unkillable process fake. **Fix:** raise `ProcessError` and keep the handle live; never claim successful termination without process exit confirmation.
2. `DurableRunStore` allowed `start_step()` after completed Runs, repeated `finish_run()` rewriting completed as failed, changing recovery status of completed/cancelled Runs, and creating attempts for nonexistent or terminal Steps. **Fix:** check status and Step identity inside the existing ledger transaction; no revision increments or partial writes on refusal.
3. Terminal completed/cancelled Run records could accept late checkpoints and Step/Attempt finalizations. **Fix:** reject those late writes under the store lock, preserving the terminal record and side-effect evidence.
4. After explicit restart reconciliation, the old interrupted Step/Attempt could be overwritten by late completion messages from the former worker. **Fix:** finalizers only accept actively `running` Step/Attempt records; stale completion is rejected. This is NOT a substitute for a cross-process execution-owner lease.

All changes are in `src/kiro_byo/process_manager.py` and `src/kiro_byo/durable_runs.py`; new tests in `tests/test_stage4_execution_integrity.py`. No changes to `crew_shared_patch.py`, Crew model selection, Tiga, UI or Stage 3 backend linkage semantics.

## Validation

- The initial 3 new regressions failed against the untouched Stage 3 code, then passed after the narrow fixes. A subsequent late-completion regression also failed before the guard and passed afterward.
- The cross-platform CI job now runs process ownership and durable recovery safety tests on **ubuntu-latest, macos-latest, windows-latest** with Python 3.12. Matrix configuration is not evidence that remote OS runners passed.
- Local development environment: Linux/Python (same container used to build). No real Provider API, no installed Crew E2E, no Windows/macOS E2E.
- Full-suite result: **869 passed, 1 skipped, 52 subtests passed in 29.33s** (single full Python-suite run).
- Source compilation, JavaScript syntax, release consistency and ZIP integrity are independently checked before candidate handoff.

## Known limits / next Stage 4 work

- When the OS will not terminate a process, BYO can now report the failure accurately; it **cannot guarantee** that an unkillable child is terminated. Session shutdown and remote MCP cancellation can still be best-effort.
- No exactly-once side-effect guarantee across process crashes. No cross-process execution-owner lease or complete hosted Crew TaskRunner/Heartbeat semantics were added.
- Crew 0.10.0-insider.5 exact source contract remains pending. Native Windows/macOS/Linux Crew installation, upgrade, uninstall, member dispatch, MCP permission and Cron tests remain required before replacing a production profile.
- Further Stage 4 iterations should use fault injection to test OS tree termination, safe MCP cancellation and early task abort, recoverable state reconciliation, and failures during durable checkpoint persistence.

## Packaging

Deliver a **single outer ZIP** containing the end-user candidate Release ZIP, full Source ZIP, this report, a SHA-256 checksum manifest, and a test-output summary. Installers should extract only the appropriate nested archive in an isolated Crew profile. Do not clear the Crew data directory to apply an upgrade.
