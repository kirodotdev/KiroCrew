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
| asyncio loop callback | A Windows Proactor reset repeated by its `connection_lost` close callback is warning-only; task-level connection resets and other exceptions remain ERRORs with crash breadcrumbs |

## Backend Error Classification

`acp/client.py` rewrites raw JSON-RPC backend errors into actionable user text
(`_format_acp_error`) and decides retry-eligibility (`_is_transient_raw_error`).
Both key off the SAME module-level `_RE_*` patterns so wording and retry verdict
never drift. Notable terminal (non-retryable) classes:

- **Malformed request**: a structural rejection (backend "Improperly formed
  request", #6022). Classified TERMINAL: the identical payload cannot succeed on
  retry, so the message states the request was malformed and points at a repair
  affordance (`/compact` to shrink and repair the conversation, or starting a new
  conversation) rather than suggesting a retry. The reset affordance is PROSE,
  not a command: this formatter does not know which surface renders the string,
  and the reset command differs per surface (`/new` on Telegram and Discord, a
  new tab on the dashboard), so naming one spelling hands every other surface's
  user a command that does nothing (#7213). A command may be named here only if
  every surface UNDERSTANDS it: `/compact` qualifies because it reaches the
  backend through the prompt transport everywhere, even on Slack, which also
  offers `!compact` as its own alias. The same rule governs the sibling
  prompt-busy branch, which for the same reason now names no command at all.
- **Usage limit** and **model not entitled**: allowance spent, or the plan lacks
  the model; also terminal, with guidance to switch model or tier.
