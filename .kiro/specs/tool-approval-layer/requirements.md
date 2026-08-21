# Requirements — Human-in-the-Loop Tool-Approval Layer

## Introduction

Kiro Crew already intercepts every agent tool call before it executes. The backend
`HookManager.on_tool_call` gate (`src/kiro_crew/hooks.py`) classifies each PreToolUse
event as `TOOL_ALLOW`, `TOOL_AUTO_APPROVE`, or `TOOL_DENY` — enforcing sensitive-path,
exfiltration, write-protected-config, deny-by-default-shell, and governance rules
*before* the tool runs. When a call is neither auto-approved nor denied, the frontend
surfaces it for a human decision: `ApprovalCard.tsx` renders the pending call, the
operator approves or rejects, and the decision flows back through
`onApprove(decision, pattern?)` → `api.approveChatSlot(slot, action, extra)`
(`ChatInput.tsx`) to resume the paused turn.

This is Kiro Crew's **batch-approve + inline tool-request preview** differentiator: a
verification checkpoint that sits between an autonomous agent and any consequential
action. The trust story is that delegation is safe *because* interception is always
running — the operator can intervene with full context instead of crossing their fingers.

The **Tool-Approval Layer** formalizes that capability as a **portable pattern**:
intercept a tool call before execution, render an approval card via generative UI,
resume on the operator's click. It is expressed against the Vercel AI SDK's tool-call
lifecycle (`UIToolInvocation` states and interrupted tool execution + client resume) so
the same shape lands in any AI-SDK app, not only Kiro Crew's dashboard.

Scope is the interception → render → resume loop and its contracts. It is NOT a rewrite
of the existing `on_tool_call` security gate (that gate remains the enforcement authority),
NOT a new approval transport (it reuses `approveChatSlot`), and NOT a change to how tools
are defined or executed.

## Requirements

### Requirement 1 — Interception before execution

**User Story:** As an operator, I want every consequential tool call paused before it
runs, so that no side effect happens without a decision I could have made.

#### Acceptance Criteria
1. WHEN an agent emits a tool call THEN the layer SHALL evaluate it through the existing `HookManager.on_tool_call` gate BEFORE the tool executes.
2. WHEN the gate returns `TOOL_DENY` THEN the call SHALL be blocked and SHALL NOT reach the approval surface (a denied call is not a pending decision).
3. WHEN the gate returns `TOOL_AUTO_APPROVE` THEN the call SHALL execute without a human prompt (reads and pre-authorized tools do not nag).
4. WHEN the gate returns `TOOL_ALLOW` (neither auto-approved nor denied) THEN the call SHALL be held as a pending decision and surfaced for human approval before it executes.
5. IF the gate cannot verify a shell tool's command (deny-by-default) THEN the call SHALL be denied, not surfaced as an approvable pending decision.

### Requirement 2 — Approval card rendered via generative UI

**User Story:** As an operator, I want the pending call rendered as a rich, contextual
card, so that I understand what the agent is about to do instead of reading a raw args dump.

#### Acceptance Criteria
1. WHEN a call is held as a pending decision THEN the layer SHALL render an approval card describing the tool, its purpose/title, and its input.
2. WHERE a tool-view schema matches the pending call's input THE card SHALL render a rich, typed preview of the input in place of the raw `<pre>` dump as the default view.
3. WHERE no schema matches THE card SHALL fall back to the current `ToolInputPreview` `<pre>` rendering with no regression.
4. WHERE a rich preview is shown THE exact, unmodified raw tool input SHALL remain available to the operator one interaction away (an expandable "show raw input" control), because a rich renderer is lossy and tool input is attacker-influenceable — a consequential argument must never be hidden by the summary.
5. IF the approval card is rendered THEN its controls SHALL be keyboard operable and expose appropriate ARIA roles/labels (WCAG AA), because operators batch-approve.

### Requirement 3 — Resume on decision

**User Story:** As an operator, I want my approve/reject click to resume the exact paused
turn, so that the agent continues with my decision applied and no state is lost.

#### Acceptance Criteria
1. WHEN the operator approves a pending call THEN the decision SHALL flow through the existing `onApprove(decision, pattern?)` callback and the paused tool call SHALL execute.
2. WHEN the operator rejects a pending call THEN the tool SHALL NOT execute and the agent turn SHALL resume with the rejection recorded.
3. WHEN an approval decision is submitted from a chat slot THEN it SHALL resolve via the existing slot-scoped `api.approveChatSlot(slot, action, extra)` path in `ChatInput.tsx` — the layer SHALL NOT introduce a new approval API path.
4. WHEN a decision resumes a turn THEN the resumed state SHALL be the same interrupted invocation (same slot, same tool-call id), never a re-issued or duplicated call.
5. IF the operator provides an approval `pattern` (e.g. "always allow this shape") THEN it SHALL be forwarded verbatim as the `extra`/`pattern` argument the existing path already accepts.

### Requirement 4 — Batch approval

**User Story:** As an operator supervising an autonomous run, I want to approve or reject
several pending calls at once, so that supervision scales past one-click-per-call.

#### Acceptance Criteria
1. WHERE more than one call is pending in a slot THE layer SHALL present them such that the operator can act on multiple pending decisions in one interaction.
2. WHEN a batch decision is submitted THEN each included pending call SHALL resolve through the same per-call `approveChatSlot` path (batch is a UI affordance over the existing per-call resume, not a new transport).
3. WHEN a batch approval includes a call the gate would deny THEN that call SHALL still be denied — batch approval SHALL NOT override the security gate.
4. IF a batch spans calls of differing risk THEN a denied or ineligible call SHALL be visibly excluded from the batch rather than silently approved.

### Requirement 5 — Portable AI-SDK lifecycle mapping

**User Story:** As an app author on the Vercel AI SDK, I want this approval loop expressed
in AI-SDK terms, so that I can adopt the pattern in my own app without Kiro Crew internals.

#### Acceptance Criteria
1. WHEN the layer models a tool call's state THEN it SHALL map to the AI-SDK `UIToolInvocation` lifecycle union (`input-streaming` | `input-available` | `output-available` | `output-error`) as the component-internal state model.
2. WHERE the AI SDK exposes an interruption/resume seam (a tool whose execution is interrupted pending client input, resumed via `addToolResult`/a continued stream) THE layer's resume step SHALL be documented against that seam so a non-Kiro-Crew app can wire the same intercept → render → resume loop.
3. WHEN the pattern is documented THEN it SHALL state explicitly which parts are Kiro-Crew-specific (the `on_tool_call` gate, `approveChatSlot`) and which are the portable shape (intercept before execute, render a card from the invocation state, resume on decision), so an adopter substitutes their own enforcement and transport without misreading the boundary.
4. WHERE Kiro Crew's transport differs from AI-SDK's typed part stream THE mapping SHALL NOT claim a wire-format equivalence that does not exist — the AI-SDK lifecycle union is reused as a state model, not as a transport claim (consistent with the App Builder Kit tool-view encoding).

### Requirement 6 — Enforcement authority is unchanged

**User Story:** As a maintainer, I want the approval layer to add UI and resume mechanics
only, so that the security guarantees keep living in one audited place.

#### Acceptance Criteria
1. WHEN the layer decides whether a call is approvable THEN the decision SHALL come from `HookManager.on_tool_call` (and the governance ceiling ∩ profile it already applies), NOT from a second, parallel policy in the frontend.
2. WHEN a call is denied by the gate THEN the layer SHALL NOT expose an operator affordance that would execute it anyway (no frontend override of a backend deny).
3. WHEN a governance or security deny fires THEN the existing SEL audit trail SHALL record it unchanged — the layer SHALL NOT bypass or duplicate audit emission.
4. IF the layer needs richer per-call metadata to render a card THEN it SHALL derive that from data already on the PreToolUse event (title, purpose, input), NOT by widening what the agent can self-authorize.

### Requirement 7 — Non-regression and CI compliance

**User Story:** As a maintainer, I want the layer to satisfy the repo's CI gates, so that
adopting it does not create review or build friction.

#### Acceptance Criteria
1. WHEN the approval card introduces a user-visible surface change THEN the change SHALL ship with committed, SHA-pinned screenshots satisfying the Screenshot Evidence gate (pending, approved, rejected, and raw-input-expanded states).
2. WHEN layer code adds user-facing strings THEN the corresponding keys SHALL exist in all locale files (catalogParity).
3. WHEN layer code is added THEN it SHALL NOT introduce copy/paste clones that fail the jscpd 0% threshold, and SHALL reuse `ApprovalCard`/`ToolInputPreview`/`ChatInput` rather than forking them.
4. WHEN the existing approval flow is exercised THEN it SHALL show no regression versus its pre-change behavior (the auto-approve, deny, and single-approve paths keep working).
