# Kiro BYO 0.2.3 · Stage 2 ACP / Crew compatibility hardening

Date: 2026-10-25. Starting point: `Kiro_BYO_0.2.3_Linkage_Stage1_Candidate_Source.zip`. This is a separate **candidate**, not a verified replacement for a production Crew installation. The original Fix 03 and Stage 1 archives remain unchanged.

## Implemented and verified in source

1. `src/kiro_byo/acp_server.py`: `session/set_model` rejects unknown sessions and invalid/empty model IDs with JSON-RPC `-32602` instead of returning `{}`. It continues to accept valid legacy model changes.
2. Both `session/set_model` and `session/set_config_option` acquire the existing per-session nonblocking turn lock before state changes, persistence and config notifications. In-flight prompt/model loops hold this lock, so competing configuration requests return `-32002`. No implicit queue or forced hot switch is introduced. Existing host explicit backend selection, `select_provider_backend()`, `member_acp_backend`, and user data ownership remain untouched.
3. `src/kiro_byo/acp_conformance.py`: separately identifies the legacy `session/set_model` compatibility method instead of presenting it as new standard ACP.
4. `tests/test_acp_stage2_contract.py`: regression tests reproduced four failures before the patch and verify negative responses, active-turn fencing, valid legacy selection and capability honesty after the patch. Tests isolate `KIRO_BYO_HOME` so diagnostic data cannot contaminate release artifacts.
5. `tests/test_acp_stdio_process.py`: starts an actual BYO ACP CLI child process and exercises JSON-RPC `initialize`, invalid model changes and unknown-method responses on stdio without hitting any provider API.
6. `.github/workflows/ci.yml`: added `acp-contract-cross-platform` for Windows/macOS/Linux × Python 3.11/3.12/3.13. Adding a job does **not** mean those remote workflows have been run.

## Validation results in current Linux container

- ACP-focused conformance and lifecycle suite: **72 passed**.
- Remaining Python suite, except 2 browser-oriented modules: **849 passed, 1 skipped, 52 subtests passed**.
- Browser-oriented modules executed separately: **6 passed**.
- Combined split-test coverage: **855 passed, 1 skipped, 52 subtests passed**.
- `compileall` and `release_check.py --metadata-only` passed. Real ACP subprocess smoke passed.
- An attempted monolithic `pytest -q` exceeded the runner timeout around 80% and is **not** claimed to have passed; test groups were then run separately. A generated runtime trace caused an initial release-tree check failure, subsequently traced to a new test lacking a temporary home. That test was fixed and all trace artifacts removed; release-tree checks then passed.

## Unverified boundaries (release blockers)

- The archive's semantic KiroCrew verifier still targets the audited 0.10 insider.4 source shape; the exact insider.5 full checkout was not available in this environment. No compatibility inference from a similar branch or newer public documentation is permitted.
- Three-platform real installed-Crew E2E, source patch installation/rollback, agent member dispatch, Cron, TaskRunner, Heartbeat, sandbox/OS keychain, and saved-member-spec parity still require independent verification. CI matrix is a planned check, not evidence of green remote runs.
- Automated global backend following across all jobs is **not implemented in Stage 2**. Stage 1 safe manual member synchronization and read-only task display remain; blanket backend mutation would violate explicit task overrides.

## Safe continuation

Run `python scripts/verify_crew10_integration.py --crew-package <exact-tag-checkout>/src/kiro_crew` on the intended target release before installing. On each operating system, test normal Kiro chat, KAS chat and member DM, BYO new/load/prompt and tool permissions, Agent/Script Cron, cancellation, resume, then switch back and uninstall; compare Crew source hashes and original data unchanged. Do not install over the sole live profile without a backup.
