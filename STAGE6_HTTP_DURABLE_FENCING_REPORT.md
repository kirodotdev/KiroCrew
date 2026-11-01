# Kiro BYO 0.2.3 — Stage 6: HTTP MCP cancellation fencing and durable ledger integrity

Date: 2026-10-25. Baseline: **exact Stage 5 combined delivery ZIP** (`Kiro_BYO_0.2.3_Stage5_完整交付包.zip`). Previous Stage 5 package remains unchanged. Engineering candidate, not an installed Crew E2E release.

## Reproduced before modifying the baseline

1. Closing an HTTP MCP session left an in-flight `tools/call` blocked until its remote HTTP response or 120-second deadline, even when its ACP session had already closed.
2. A closed `HttpMcpServer` could be started again, dispatching new requests outside its intended session lifetime.
3. `DurableRunStore.finish_attempt()` and `finish_step()` could acknowledge unknown IDs as successful no-ops, increasing the persisted revision and creating misleading completion evidence.

All three new cases **failed against Stage 5** before the minimal fixes; then passed. One additional test verifies saturation of 8 concurrent HTTP requests and prompt local release on session close.

## Changes

- HTTP MCP transport executes blocking `urllib` work in bounded (8-per-server) daemon workers. The ACP caller waits interruptibly, so `close()` wakes callers promptly; late responses are **never delivered to the retired MCP session**. Closed server instances reject new calls. `start()` is fenced against concurrent shutdown.
- The HTTP request ID counter uses a dedicated lock to prevent concurrent allocations from using a mutable shared counter without synchronization.
- Durable Attempt and Step completions now reject unknown IDs and do not mutate the ledger revision. Attempt completions also refuse to act on a non-running parent Step.
- Added cross-platform CI test job (Python 3.11/3.12/3.13 × Linux/macOS/Windows), and regression tests under `tests/test_stage6_http_and_durable_fencing.py`.

## What these guarantees do NOT mean

- **Local cancellation is not remote rollback.** A cancelled HTTP request may still finish on the remote server. Its side effects have *unknown* outcome; no automatic retry should assume otherwise.
- This implementation does not forcibly interrupt a blocked `urllib` operation. The daemon network worker may remain alive until the HTTP timeout/server response (or longer in an OS-level stall). The bounded concurrency limit prevents unbounded per-session growth. This is not network-level cancellation or at-most-once execution.
- Cross-process lease ownership/heartbeats and side-effect idempotency keys remain pending, deliberately not implemented as unverified fields. Crew Cron/TaskRunner actual Harness selection is unchanged.
- Windows/macOS runners are now configured in CI, but **were not executed** in this container; real KiroCrew installation, upgrade and E2E testing on all platforms still need evidence.

## Verification

- Red tests: 3 fail in untouched baseline, validating reproduced gaps.
- Focused new and legacy tests: **51 passed**.
- Full Linux suite: **878 passed, 1 skipped, 52 subtests passed**, exit code 0 (`python -m pytest -q`, from clean Stage 5 extraction after modifications).
- Python compile, JS syntax, release metadata check and archive CRC/SHA-256 are required before delivery.
- No change to Crew native backend selection, Tiga, Auto/Jev hiding, Stage 3 linkage, Provider adapters or user-installed Crew data.
