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
