# Native Prompt Optimizer

## Overview

Pre-send prompt optimization is an explicit compose-bar action. When enabled for a `ChatInput` host, `Cmd/Ctrl+Shift+Enter` and the sparkle button rewrite the draft for scope, specificity, and structure before it is sent to the agent. `ChatInput.optimizePrompt` never sends the result; it writes the rewrite back to the draft for review, editing, or discard.

There is no client- or server-side heuristic that decides whether a nonempty prompt merits optimization. `handle_optimize` returns empty drafts directly, while `OPTIMIZER_SYSTEM` instructs the model to leave prompts that are already specific, scoped, and actionable unchanged. The handler also marks a case-insensitive match as unchanged, so cosmetic casing cannot replace a draft.

## Backend

`dashboard/handlers/optimizer.py` serves `POST /api/optimizer/optimize`; `dashboard/routes/chat.py::register_chat_routes` registers it through `handlers.handle_optimize`. The request contains `prompt`, `context`, and optional `pastes`; the response contains `optimized` and `changed`.

- `handle_optimize` acquires a dedicated `_optimizer` `kirocrew-lite` session rather than sharing the background session. The dedicated key keeps an on-demand edit from queuing behind background side work; the nested `_optimize` coroutine releases the session in `finally`, and its enclosing `asyncio.wait_for` bounds acquire, streaming, and release together.
- The stream accepts text chunks until `EVENT_COMPLETE` and rejects every `EVENT_PERMISSION_REQUEST` with `client.reject_tool`. Rejecting permission requests keeps the side session from turning a draft rewrite into an authorized tool action.
- Invalid JSON returns HTTP 400. Empty drafts, injection-screening hits, timeout or stream failures, empty output, the `UNCHANGED` sentinel, and rejected paste-placeholder rewrites return the original draft with `changed: false`; `TestOptimizerEndpoint` and `TestOptimizerPasteHandling` cover these fail-soft outcomes.
- Model replies are normalized before every downstream check: surrounding quotes and at most one XML-style wrapper tag enclosing the whole reply are stripped (`_strip_outer_wrapper_tag`), so a format-imitating wrapper neither lands in the draft nor makes an otherwise-identical rewrite compare as changed. Only a leading identifier-shaped tag matched by its own closing tag at the very end of the reply is removed — one layer at most, text that merely contains angle brackets is untouched, and a tag the draft itself contains is never treated as a wrapper, so a user's own XML structure survives an echoed or rewritten reply. `TestStripOuterWrapperTag` and `TestOptimizerWrapperHandling` pin the shape.
- `handle_optimize` sends `prompt`, `context`, and the assembled paste block through `security.contains_injection` before streaming. A hit declines optimization rather than delivering untrusted text to the model; the constrained session, rejected permission requests, and output redaction reduce the impact if model output is unsafe.
- Payload sections use a fresh request nonce in their pseudo-XML names. `handle_optimize` keeps untrusted text separate from the delimiter construction, so supplied prompt, context, and paste content do not choose the section boundary.
- `handle_optimize` redacts exfiltration URLs and credentials from model output with `redact_exfiltration_urls` and `redact_credentials` before responding.
- Context is bounded at both ends: `ChatInput.optimizePrompt` selects and shortens recent user and assistant messages, and `handle_optimize` retains only the context tail. `TestOptimizerEndpoint.test_context_truncated_to_2000_chars` pins the server-side bound.

## Paste forwarding

The chat input collapses a large paste into an inline `[ Paste #N · M lines ]` placeholder and retains the source text in a `PasteBlock` list in `website/src/utils/pasteTokens.ts`. `ChatInput.optimizePrompt` forwards only blocks referenced by the current draft, so the optimizer can use the content for scope without expanding it into the rewritten draft.

`_build_pasted_content_block` accepts only referenced, well-formed blocks, keeps the first block for each sequence, orders them by sequence, and bounds both scanned blocks and forwarded content. The content limit protects model context and request latency; `TestBuildPastedContentBlock.test_truncates_over_budget` pins its truncation behavior. Malformed or unreferenced `pastes` entries produce no forwarded section instead of failing the request.

**Placeholder preservation is enforced, not trusted.** `handle_optimize` compares the multisets of complete placeholder strings before returning a rewrite. This is load-bearing because `pasteTokens.ts` expands by the exact token text per occurrence: dropping, duplicating, or changing a line-count suffix would lose content, expand it twice, or leave an unexpanded token. `TestOptimizerPasteHandling.test_dropped_placeholder_returns_original`, `test_duplicated_placeholder_returns_original`, and `test_altered_linecount_placeholder_returns_original` pin the guard.

## Frontend

`website/src/components/ChatInput.tsx` uses a React Query mutation to call the endpoint. The keyboard shortcut is recognized only when the host enables `promptOptimizer` and the composer is connected; the sparkle button uses the same `optimizePrompt` callback.

- `promptOptimizer` defaults to enabled, but a host can disable the feature. `SideChat` does so because it does not provide the cross-slot result route; `ChatInput.paste.test.tsx` pins that the opt-out covers the shortcut.
- The textarea overlay and `readOnly` state are scoped to the slot that started the request. `optimizePendingRef` still prevents a second request from another displayed slot, so a pending mutation cannot clobber its own lifecycle state.
- `optimizeSlotRef` binds a completion to its originating slot. When a host provides `onOptimizeResult` and the user changes slots, `ChatInput` passes the result or the original fallback to that callback; `useChatPageResourcesController.tsx::handleOptimizeResult` persists it in the originating draft. `ChatInput.test.tsx` pins that a late completion cannot overwrite the visible different slot.
- Before an in-place write, `setTextUndoable` compares the current trimmed draft with the submitted draft and drops a mismatch rather than clobbering a later edit. It attempts `document.execCommand('insertText')`, verifies the resulting DOM value, and reconciles through `onChange` when the browser does not insert; `ChatInput.optimizeWriteback.test.tsx` pins those fallback paths and the undo-history boundary.

## Config

There is no persisted optimizer setting. `ChatInput` enables `promptOptimizer` by default, while embedding hosts choose whether the capability is appropriate for their surface.

## Key files

- `src/kiro_crew/dashboard/handlers/optimizer.py`: endpoint, system prompt, paste-block assembly, input screening, output redaction, and placeholder guard.
- `src/kiro_crew/dashboard/routes/chat.py`: optimizer route registration.
- `website/src/components/ChatInput.tsx`: capability gate, shortcut, mutation, slot routing, and verified write-back.
- `website/src/pages/chat/useChatPageResourcesController.tsx`: persistence of a late cross-slot result into its originating draft.
- `website/src/utils/pasteTokens.ts`: placeholder format and the `PasteBlock` model shared with the backend regex.
- `test/test_optimizer.py`: backend behavior, context bound, paste forwarding, placeholder preservation, and injection-screening cases.
- `website/src/test/ChatInput.optimizeWriteback.test.tsx`: verified write-back fallback and undo-boundary cases.
