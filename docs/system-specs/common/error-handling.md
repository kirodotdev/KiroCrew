# Error Handling

## Principles

1. Custom exceptions in `acp/client.py` for ACP-protocol errors, and in
   `acp/session_handle.py` for runtime/transport errors
2. Error strings at CLI boundaries (never expose tracebacks to users)
3. Graceful degradation — partial output returned on timeout

## Exception Hierarchy

Two independent families. `AcpError` covers protocol and prompt-level failures;
`AcpRuntimeError` covers the process and request transport underneath it.

```
AcpError (base, acp/client.py)          — carries `transient`, the retry verdict
├── AcpTimeoutError        — prompt timed out, has partial_output
├── AcpPermissionNeeded    — tool approval required
├── AcpProcessDied         — kiro-cli exited unexpectedly
├── AcpAuthRequired        — kiro-cli not authenticated; non-retryable
├── AcpToolGateUnroutable  — tool calls would bypass the PreToolUse gate;
│                            non-retryable, wraps acp_tool_gate.ToolGateUnroutable
├── AcpModelUnavailable    — requested model not entitled; non-retryable
└── AcpPromptBusy          — a prompt is already in flight on this session

AcpRuntimeError (base, acp/session_handle.py)
├── AcpRuntimeDead            — the underlying process has died
├── AcpRequestTimeout         — a request's response missed its budget
└── AcpWorkspaceBindingError  — a descriptor-bound runtime cannot serve another
                                cwd (acp/runtime.py)
```

`AcpToolGateUnroutable` is a distinct type rather than a transport error because
the condition is a configuration fact: a respawn re-reads the same answer and
refuses again while consuming a reconnect budget meant for transport faults. The
same argument makes `AcpAuthRequired` and `AcpModelUnavailable` distinct — each
one is invalid on its own terms, so the retry ladder must be skipped rather than
walked. `AcpRequestTimeout` subclasses its base so existing
`except AcpRuntimeError` handlers keep catching it.

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
  request"). Classified TERMINAL: the identical payload cannot succeed on
  retry, so the message states the request was malformed and points at a repair
  affordance (`/compact` to shrink and repair the conversation, or starting a new
  conversation) rather than suggesting a retry. The reset affordance is PROSE,
  not a command: this formatter does not know which surface renders the string,
  and the reset command differs per surface (`/new` on Telegram and Discord, a
  new tab on the dashboard), so naming one spelling hands every other surface's
  user a command that does nothing. A command may be named here only if
  every surface UNDERSTANDS it: `/compact` qualifies because it reaches the
  backend through the prompt transport everywhere, even on Slack, which also
  offers `!compact` as its own alias. The same rule governs the sibling
  prompt-busy branch, which for the same reason now names no command at all.
- **Usage limit** and **model not entitled**: allowance spent, or the plan lacks
  the model; also terminal, with guidance to switch model or tier.
