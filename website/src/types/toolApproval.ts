import type { PendingApproval } from './index'

// Human-in-the-Loop Tool-Approval Layer — shared frontend model.
//
// A `PendingDecision` is the frontend view of one tool call paused at the
// PreToolUse boundary, awaiting an operator's approve/reject. It adds NO new
// backend wire field: it carries the existing `PendingApproval` event verbatim
// (`approval`, from types/index.ts — `tool`, `tool_input`, `tool_kind`,
// `request_id`) plus the two pieces of context the event itself does not hold:
//   - slot        ← the chat slot the paused turn belongs to
//   - toolCallId  ← `tool_call_id` when a tool-activity event carries it
//                   (spread conditionally by useWebSocket.ts); optional, used for
//                   display/correlation only. It is NOT the resume identifier.
// Carrying `PendingApproval` (rather than re-spelling its fields) keeps the model
// pinned to the real wire shape: if that shape drifts, `tsc` fails here.
//
// Resume identifier: `approval.request_id`. Plain approve/reject resolves through
// the id-scoped `api.resolveApproval(request_id, action)` → POST
// /api/approvals/<id>/<action> (ChatInput.tsx). The slot-scoped
// `api.approveChatSlot(slot, …)` is used ONLY for trust grants, and downgrades to
// `resolveApproval` for unattended sources — so this layer resumes by
// `request_id`, never by inventing a new call. A stale/closed id is a no-op.
//
// `ToolInvocationState` mirrors the shape of the Vercel AI SDK `UIToolInvocation`
// lifecycle as a STATE MODEL ONLY — a way to reason about the phases of one tool
// call, NOT a claim that Kiro Crew's transport carries a typed `tool-<name>` part
// stream (it does not; the transport is markdown + <mcwidget> opaque strings).

/**
 * The lifecycle phase of a single tool invocation.
 *
 * `input-available` is the approvable moment: the input is complete but the tool
 * has not run. The approval card renders from this state; a decision moves it to
 * `output-available` (executed) or `output-error` (rejected / failed).
 *
 * `input-streaming` is intentionally omitted: on this transport the pending
 * event arrives with the input already complete, so there is no partial-args
 * phase to model. Inputs/outputs are `unknown` here — Task 0 has no
 * schema-matched consumer; a typed variant returns with Task 1's typed preview
 * (`kit/tool-views`), the first caller that can name a concrete type.
 */
export type ToolInvocationState =
  | { state: 'input-available'; input: unknown } // ← the approvable moment
  | { state: 'output-available'; input: unknown; output: unknown }
  | { state: 'output-error'; input: unknown; error: string }

/**
 * One tool call awaiting a human decision. Wraps the existing `PendingApproval`
 * event (no new backend wire field) and the invocation lifecycle.
 */
export interface PendingDecision {
  /** Chat slot the paused turn belongs to; scopes display/routing. */
  slot: string
  /**
   * The verbatim backend approval event. `approval.request_id` is the resume
   * identifier (`api.resolveApproval`); `approval.tool_input` is the ground-truth
   * "show raw input" source; `approval.tool` is the display title.
   */
  approval: PendingApproval
  /**
   * `tool_call_id` when a tool-activity event carries it (spread conditionally by
   * the backend). Optional; for display/correlation only — resume uses
   * `approval.request_id`, not this. A missing id is never a re-issued call.
   */
  toolCallId?: string
  /** The invocation lifecycle; at the approvable moment this is `input-available`. */
  invocation: ToolInvocationState
}
