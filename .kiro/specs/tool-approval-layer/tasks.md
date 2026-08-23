# Implementation Plan — Human-in-the-Loop Tool-Approval Layer

This plan enriches the render + resume steps of Kiro Crew's existing approval loop and
documents the portable AI-SDK mapping. The backend enforcement gate (`on_tool_call`) is
NOT modified. **Prerequisite:** the App Builder Kit's `kit/tool-views` module
(`ToolPreviewFrame` + `defineToolView`) must ship first — this spec is sequenced after it
and Task 0 verifies it fail-closed (Req 8). The Req 2.3 `<pre>` fallback path can deliver
Reqs 1/3/4/5/6 without the module; only the typed rich preview (Req 2.2) is gated on it.

- [ ] 0. Verify the tool-views prerequisite, then establish the `PendingDecision` frontend model
  - **Prerequisite check (fail-closed, Req 8.1/8.3):** confirm the App Builder Kit
    `website/src/kit/tool-views/` module exists and exports `ToolPreviewFrame` +
    `defineToolView`. If it does not, STOP and report the dependency — do NOT stub a local
    `ToolPreviewFrame` (that forks the App Builder Kit contract). The Req 2.3 `<pre>`
    fallback path may proceed for Reqs 1/3/4/5/6 without it (Req 8.4).
  - Define `PendingDecision` (`slot`, `toolCallId`, `title`, `rawInput`, `invocation`) and
    the `ToolInvocationState` union in a shared types module, mirroring AI-SDK
    `UIToolInvocation` as a state model only.
  - Confirm (via reading `useWebSocket.ts` + `ChatInput.tsx`) that a `PendingDecision` can
    be assembled from the existing PreToolUse event with NO new backend wire field, and that
    `approveChatSlot(slot, action, extra)` already pins resume to the slot.
  - _Requirements: 3.3, 3.4, 5.1, 6.4, 8.1, 8.3_

- [ ] 1. Rich preview inside `ApprovalCard` (default view, with raw-input fallback)
  - Host `ToolPreviewFrame` (from `kit/tool-views`) inside `ApprovalCard`/`ToolInputPreview`.
  - Render the typed preview when a schema matches the pending input; fall back to the
    existing `<pre>` when none matches.
  - Add an always-present, collapsed "show raw input" control revealing verbatim `rawInput`.
  - _Requirements: 2.1, 2.2, 2.3, 2.4_

- [ ] 2. Keyboard + ARIA on the approval controls
  - Make approve / reject / pattern controls keyboard operable with correct ARIA roles and
    labels (WCAG AA); verify tab order and focus management for batch operation.
  - _Requirements: 2.5_

- [ ] 3. Wire the resume path (approve / reject / pattern)
  - Route the decision through the existing `onApprove(decision, pattern?)` →
    `api.approveChatSlot(slot, action, extra)`; forward `pattern` verbatim as `extra`.
  - Ensure resume targets the same `slot` + `toolCallId`; a stale/closed id is a no-op with
    a surfaced notice, never a re-issued call.
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5_

- [ ] 4. Batch approval affordance
  - Render a multi-select when N calls are pending in a slot; submitting a batch iterates
    the per-call `approveChatSlot` resume (no new transport).
  - Surface any per-call rejection the gate returns at resume as excluded **after the fact**
    (backend-driven, not predicted); never pre-filter by forecasting the verdict, and never
    silently approve an excluded call.
  - _Requirements: 4.1, 4.2, 4.3, 4.4_

- [ ] 5. Enforce the single-authority boundary
  - Assert the frontend never renders an approvable card for a `TOOL_DENY` verdict and
    exposes no affordance that would execute a denied call.
  - Confirm the SEL audit for a deny still fires unchanged (no bypass, no duplication).
  - _Requirements: 6.1, 6.2, 6.3_

- [ ] 6. Document the portable AI-SDK mapping
  - Write the intercept → render → resume mapping table naming the Kiro-Crew-specific parts
    (`on_tool_call`, `approveChatSlot`) vs the portable shape, and the AI-SDK resume seam
    (`addToolResult` / continued stream).
  - State explicitly that the `UIToolInvocation` union is a state model, not a transport
    equivalence (Kiro Crew's transport is markdown + `<mcwidget>` opaque strings).
  - _Requirements: 5.1, 5.2, 5.3, 5.4_

- [ ] 7. Tests — render, resume, batch, non-bypass
  - Unit (vitest): each `PendingDecision` state; schema-match vs `<pre>` fallback;
    raw-input reveal; keyboard/ARIA.
  - Resume: approve/reject route with correct `slot`/`toolCallId`/`pattern`; stale id no-op.
  - Batch: per-call resolution; a gate-rejected call surfaced as excluded after the fact.
  - Non-bypass: a `TOOL_DENY` never yields an approvable card.
  - Backend (pytest): reuse `test_dashboard_approval.py` / `test_auto_approve.py` /
    `test_approval_threading.py` to assert gate verdicts + SEL audit unchanged.
  - _Requirements: 2.*, 3.*, 4.*, 6.*, 7.4_

- [ ] 8. CI compliance + non-regression
  - Capture SHA-pinned screenshots of pending / approved / rejected / raw-expanded states
    for the Screenshot Evidence gate.
  - Land all new `i18nT()` keys in every locale file (catalogParity).
  - Reuse `ApprovalCard`/`ToolInputPreview`/`ChatInput` (jscpd 0%); confirm the
    auto-approve, deny, and single-approve paths show no regression.
  - _Requirements: 7.1, 7.2, 7.3, 7.4_
