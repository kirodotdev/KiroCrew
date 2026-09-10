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

## Dashboard Error Codes

Dashboard JSON errors include a stable lower-snake `code` alongside advisory
`error` text, preserving the route's HTTP status. Redact untrusted text fields
before putting them in a transparent response dictionary. A computed status is
compliant when that dictionary carries an explicit code; an uncoded or opaque
body remains debt in `test/test_error_code_contract.py`. That guard also checks
literal code values on computed-status responses and refuses dictionary spreads
that could replace the code.

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

The auth family has exactly ONE transient member, **credential propagation**.
Bedrock refuses a freshly minted credential with "The security token included in
the request is invalid" — usually wrapped in `UnrecognizedClientException` and
carrying a 403 — until IAM has propagated it, and the identical credential is
accepted seconds later, so the existing backoff ladder absorbs it.
`is_credential_propagation_delay` is the single predicate, read ahead of
`_RE_AUTH` and the session-expiry branch in BOTH the classifier and the
formatter, ahead of `llm_helpers._is_transient_acp_error`'s
`accessdenied`/`unrecognizedclient` exclusion short-circuit (a
`_TRANSIENT_MARKERS` entry alone is unreachable, because the exclusion sits
above the markers), and inside `is_auth_failure_output` so `acp/runtime.py`'s
stderr latch does not convert it to the explicitly non-retryable
`AcpAuthRequired` and skip the ladder entirely. It is scoped to the "is invalid"
WORDING, never to the status: a bare 401/403, an `... is expired` token, a
combined "invalid or expired", and an invalid *bearer* token all stay terminal.
The predicate lives in `credential_errors.py`, not `acp/client.py`, so consumers
on the application side of the agent-SDK boundary share one verdict without a
fresh ACP-layer import edge.

That wording is shared with a permanently invalid access key
(`UnrecognizedClientException` or `InvalidClientTokenId` for a key that was
deleted, rotated, or mistyped), so a never-valid credential is also classified
transient. The retry budget bounds that misclassification — three retries, ~15 s
— and the formatted message closes with the terminal "refresh your AWS
credentials" guidance rather than asserting the propagation diagnosis as fact.

Retry hints are the other wording-only signal, and they are **provider-scoped**:
`_RE_5XX_HINT` carries one alternative per backend spelling ("please try again"
for Kiro/Bedrock, "try your request again" for the claude-agent-acp seam's
generic upstream 500, whose frame has no named exception and no HTTP status, so
the hint is its only transient marker). Onboarding a backend means auditing that
alternation. The kiro-cli mid-stream envelope ("Encountered an error in the
response stream: …") is deliberately NOT a hint — matching it would make the
branch a catch-all that discards the real cause.

## Model-Side Refusals

A refusal is a turn the model DECLINED, not a turn that failed: the request
reached the model and the answer is "no". It is deterministic — the same prompt
hits the same filter — so it is never retried, and the useful thing to show is
the reason. Harnesses report that reason unevenly, so `acp/types.RefusalInfo`
is the one shape every harness is folded onto (`category`,
`explanation`, `recommended_model`), each field left EMPTY when the provider
did not say — never guessed.

- **Kiro (kiro-cli, KAS)** — the service's content filter emits a
  `_kiro.dev/metadata` frame with `stopReason: CONTENT_FILTERED` and a `refusal`
  object, streams the canned explanation ("The selected model cannot continue
  this conversation…") as ordinary assistant text, then ends the turn with a
  plain `end_turn` (or a bare `-32603`). `acp/_dispatch.parse_refusal` reads the
  frame (members of `ACP_BACKENDS_STRUCTURED_REFUSAL` only) onto
  `AcpPromptStats.refusal`; `AcpPromptStats.terminal_refusal` rewrites the
  terminal's stop reason to `STOP_REASON_REFUSAL` and attaches the payload as
  `AcpEvent.refusal`. The explanation is redacted at the parser.
- **claude-agent-acp, codex-acp** — only Anthropic's bare `stopReason: "refusal"`
  reaches the client; `terminal_refusal` passes it through with no payload, and
  the dashboard's refusal branch (keyed on the stop reason) renders the bare card.
- **Dashboard** — `chat_runner.refusal_card_text` renders one card from
  `RefusalInfo`: the lead line, then one line per non-empty field. Because the
  Kiro explanation streams as text, the card is emitted from BOTH the answered
  and the text-less branch of the turn epilogue.
