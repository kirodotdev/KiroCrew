# Native Prompt Optimizer

## Overview

Pre-send prompt optimization, triggered by `Cmd/Ctrl+Shift+Enter` or the sparkle
button in the compose bar. The draft in the chat input is rewritten for scope,
specificity, and structure before it is sent to the agent. There is no auto-send:
the rewrite lands back in the textarea and the user reviews, edits, or discards
it.

Optimization is an explicit user action, so there is no client-side or
server-side "is this prompt worth optimizing" heuristic. The only fast-path is an
empty draft. Rule 2 of the system prompt (return an already-specific prompt
unchanged) is what prevents over-optimizing, and the handler treats a rewrite
that matches the original case-insensitively as `changed: false`.

## Backend

`dashboard/handlers/optimizer.py`, one endpoint: `POST
/api/optimizer/optimize` (registered in `dashboard/server.py`). Request body is
`{prompt, context, pastes}`; response is `{optimized, changed}`.

- **Dedicated `_optimizer` session on the `kirocrew-lite` agent.** Not
  `BACKGROUND_KEY` (`_bg`). Every `_bg` caller shares one session serialized by a
  single `Semaphore(1)`, and chat-title generation (`dashboard/chat_title.py`)
  plus folder-icon generation (`dashboard/chat_folders.py`) both run there on
  ordinary message traffic. An optimizer sharing `_bg` therefore queues behind
  work the user did not ask for and can exhaust its own 30s budget waiting for
  the semaphore. The cost of the dedicated key is one extra lightweight session;
  the benefit is that optimize latency is independent of background chat
  bookkeeping.
- **One 30s `asyncio.wait_for` wraps the whole operation**, session acquire plus
  stream plus release, so a hang anywhere inside cannot exceed the budget. A
  `try/finally` releases `_optimizer` even when the stream raises.
- **No tool calls.** Permission requests seen on the stream are rejected
  (`client.reject_tool`); the turn ends at `EVENT_COMPLETE`.
- **Every failure path returns the original prompt with `changed: false`**:
  malformed JSON (HTTP 400 instead), empty draft, timeout, any stream exception,
  empty output, and the literal `UNCHANGED` sentinel.
- **Untrusted input is screened before it reaches the model.** `prompt`,
  `context`, and the assembled paste block each go through
  `security.contains_injection`; a hit returns the draft unoptimized rather than
  letting a crafted draft try an instruction breakout. The blast radius is
  already bounded (constrained side session, tools rejected, output redacted),
  so refusing to optimize is the cheap safe answer.
- **Per-request nonce delimiters.** The payload's pseudo-XML sections
  (`<context-{nonce}>`, `<pasted_content-{nonce}>`, `<original_prompt-{nonce}>`)
  carry a fresh 12-hex-char nonce so a crafted prompt or paste cannot forge a
  closing tag and escape its data section.
- **Output is redacted** through `redact_exfiltration_urls` then
  `redact_credentials` before it is returned.
- **Context is truncated server-side to the last 2000 chars.** The frontend sends
  the last 10 user/assistant messages, each capped at 200 chars.

## Paste forwarding

The chat input collapses a large paste into an inline placeholder
(`[ Paste #N · M lines ]`, middle dot U+00B7) and keeps the real text in a
`PasteBlock` list (`website/src/utils/pasteTokens.ts`). The optimizer must be
able to scope a rewrite around that content without expanding it, so the
frontend forwards the referenced blocks in the `pastes` field and the handler
builds a separate `<pasted_content-{nonce}>` section marked as data, not
instructions.

Two constants bound that section:

| Constant | Value | Why |
|---|---|---|
| `_PASTE_CONTENT_BUDGET` | 12000 chars | Total forwarded paste text. A single paste can be a whole log or transcript; an unbounded dump would blow the lite model's context window and the 30s budget. Blocks are added in seq order and the one that crosses the budget is truncated with a `… (truncated)` marker. |
| `_MAX_PASTE_BLOCKS` | 128 | Cap on blocks scanned. The content budget bounds bytes but not list length, so a request with a huge number of tiny blocks would still pay per-block validation cost before the budget filtered any out. A real draft references a handful. |

Only blocks whose seq actually appears in the draft are forwarded, de-duped by
seq (first wins), and non-dict or non-string entries are skipped, so a malformed
`pastes` field degrades to an omitted section rather than an error.

**Placeholder preservation is enforced, not trusted.** The frontend substitutes
real content back by exact placeholder string, per occurrence, on send. So the
handler compares the *multiset of full placeholder strings* in the draft against
the rewrite and discards the rewrite on any mismatch. A seq-subset check would be
too weak in both directions: a rewrite that duplicates a placeholder would expand
the content twice, and one that alters the `· M lines` text keeps the seq present
while breaking exact-string substitution, leaving the token unexpanded in the
submitted prompt.

## Frontend

`website/src/components/ChatInput.tsx`, a React Query mutation.

- Keyboard path: `e.key === 'Enter' && (e.metaKey || e.ctrlKey) && e.shiftKey`.
  Sparkle button path: same `optimizePrompt` callback.
- While a request is in flight the textarea is `readOnly` and dimmed under a
  dark overlay, so the value cannot diverge from what was sent.
- `optimizePendingRef` guards re-entrancy on the RAW mutation lifecycle: one
  optimize per `ChatInput` instance. The slot-scoped `optimizing` flag drives UI
  only, and would read false on a session the user has navigated away from,
  which is exactly where a second request could clobber the first.
- `optimizeSlotRef` pins the slot that started the optimize. If the user
  switches sessions mid-flight, the result is routed back to the originating
  session via `onOptimizeResult` rather than written into whatever is on screen
  or silently dropped. On the originating session a trim-guard compares the
  current value against what was sent and drops the result on any mismatch
  instead of clobbering.
- The result is written with `document.execCommand('insertText')` so it forms a
  single undo boundary.

## Config

None. The feature is always available and has no config keys.

## Key files

- `src/kiro_crew/dashboard/handlers/optimizer.py`: endpoint, system prompt,
  paste-block assembly, placeholder guard.
- `src/kiro_crew/dashboard/server.py`: route registration.
- `website/src/components/ChatInput.tsx`: shortcut, sparkle button, mutation,
  slot routing.
- `website/src/utils/pasteTokens.ts`: placeholder format and the `PasteBlock`
  model shared with the backend regex.
- `test/test_optimizer.py`: backend tests, including the paste-budget,
  placeholder-multiset, and injection-screening cases.
