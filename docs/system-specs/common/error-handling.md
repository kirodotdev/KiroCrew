# Error Handling

## Principles

1. Custom exceptions in `acp/client.py` for ACP-specific errors
2. Error strings at CLI boundaries (never expose tracebacks to users)
3. Graceful degradation — partial output returned on timeout

## Exception Hierarchy

```
AcpError (base)
├── AcpTimeoutError      — prompt timed out, has partial_output
├── AcpPermissionNeeded  — tool approval required (phase 3)
└── AcpProcessDied       — kiro-cli exited unexpectedly
```

## Boundaries

| Boundary | Strategy |
|----------|----------|
| ACP → CLI | Catch `AcpError`, print user-friendly message, `sys.exit(1)` |
| JSON-RPC read | Non-JSON lines silently skipped (kiro-cli debug output) |
| Config load | Invalid JSON → log warning, return defaults |
| Process spawn | `shutil.which` check before spawn; clear error if missing |


## Quality Engineering

The native Quality Engineering Crew (`quality-engineering`) treats every unverified
quality result as blocked. Request validation rejects missing, relative, sensitive,
traversing, symlinked, or unresolvable project paths; direct `/crew` syntax and the
active slot project are validated before any workflow or provider session is used.

Evidence checks are package-registered fixed argv only. A missing executable,
unknown adapter/check, timeout, output overflow, non-zero exit, workspace-copy
failure, symlink/traversal violation, evidence persistence failure, or application
check that is only a capability probe produces a blocked result. Built-in
`playwright_cli_capability`, browser, and iOS checks are capability probes, not
application E2E passes; application E2E requires an explicitly registered check
with `evidence_kind="application_e2e"`.

Role execution is schema-validated at both input and handoff boundaries. Malformed
role output, invalid schema payloads, depth/key/item/byte bound violations, runner
exceptions, and an invalid aggregate report stop the route and return a stable
blocked reason. No partial role report is promoted to a pass, and
`full_quality_review` emits `quality_report` only after all required evidence and
role handoffs pass. User-facing dashboard acknowledgements contain bounded,
redacted text and never expose tracebacks, arbitrary argv, secrets, or raw external
output.
