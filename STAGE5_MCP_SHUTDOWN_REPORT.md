# Kiro BYO 0.2.3 — Stage 5: MCP session shutdown integrity

Date: 2026-10-25. Base: exact `Kiro_BYO_0.2.3_Stage4_完整交付包.zip`, outer SHA-256 `9fc13a01ff90347713258259cee0faa2c25e1e21b6819397b8bdec85399f96fa`. The Stage 4 original is not modified. Engineering candidate only.

## Reproduced failures

- An MCP stdio `tools/call` waiting on a 120 s JSON-RPC deadline did not wake up on session close. Shutdown could report completion while the prior call thread remained blocked, delaying turn cancellation and cleanup.
- A call on a closed `McpManager` returned an ordinary unknown-tool response instead of a clear fail-closed session lifecycle error.
- A stdio MCP server process inherited the host process group, so a subprocess tree could survive after its parent was terminated. This risked leaving MCP children outside the session lifecycle.

All three were covered by tests which **failed against untouched Stage 4** before code changes (`tests/test_stage5_mcp_shutdown.py`).

## Minimal fixes

- Transport-scoped queue and exit event; `close()` or peer EOF now wakes a pending request with `McpTransportError`. Each process generation owns its queue and reader, preventing a late response from a stale server instance being put into a restarted connection.
- `McpManager.call()` explicitly declines dispatch if its session has closed, including after a discovery wait.
- Stdio server processes start in a new session/process group on POSIX or Windows process group on Windows. Closing a running stdio server kills its owned group (`SIGKILL` on POSIX, `taskkill /T /F` on Windows; direct-parent kill fallback), then waits for parent process exit. OS refusal is logged, not passed off as confirmed cleanup.
- Client-side MCP closes do **not** guarantee undo of external mutations already executed by a tool. In-flight uncertain tool effects remain surfaced by the existing Turn Engine. HTTP requests that are already in urllib cannot be forcibly interrupted through this change.

## Deliberately out of scope

- No changes to ACP routing, KiroCrew native backend selection, Tiga/Auto/Jev compatibility, task linkage, Crew source patches, provider adapters, or DurableRunStore.
- No user profile, Crew data, or local install was modified.
- Windows/macOS CI is configured; actual remote CI jobs and platform-native Crew E2E have not been run in this environment. Crew 0.10.0-insider.5 exact upstream source contract remains unverified.

## Test / release evidence

Full Linux Python test run: **874 passed, 1 skipped, 52 subtests passed**, clean exit code 0 (second full run, 31.46 s). Focused MCP+Crew tests: **57 passed**. See top-level delivery `TEST_RESULTS.txt`, `SHA256SUMS.txt`, and `BUILD_INFO.json`. `scripts/release_check.py` gates release metadata, UI resources, and mutable-user-file exclusions. All archives undergo ZIP CRC and SHA-256 verification.
