/**
 * Parsing for the `assistant` row the gateway injects when a background
 * workflow finishes, whose markdown begins with "[Workflow completion event]".
 * Kept apart from WorkflowCompletionCard so a pure module (the app SDK's
 * status-row verdict) can ask "is this the envelope?" without importing React.
 */
import type { ChatMessage } from '../types'

const WF_COMPLETION_PREFIX = '[Workflow completion event]'
// Any char but a backtick (newlines too) may sit in the name; a name holding a
// backtick fails the parse and the row falls back to normal markdown.
const WF_COMPLETION_RE = /^\[Workflow completion event\]\s*\nWorkflow `([^`]+)` \((wf_[A-Za-z0-9_]+)\) → \*\*([a-z]+)\*\*/

/** True when an assistant message is an injected workflow completion event
 *  whose header actually PARSES. Gating on a successful parse (not just the
 *  loose prefix) is deliberate: ChatPage branches to WorkflowCompletionCard on
 *  this predicate, and the card renders null when the header can't be parsed —
 *  so a prefix-only match would swallow the completion (including the result
 *  the user was waiting for) instead of degrading to normal markdown. */
export function isWorkflowCompletionMessage(message: ChatMessage): boolean {
  if (message.role !== 'assistant') return false
  const content = message.content || ''
  if (!content.startsWith(WF_COMPLETION_PREFIX)) return false
  return parseWorkflowCompletion(content) !== null
}

interface ParsedCompletion {
  name: string
  runId: string
  status: string
  /** Markdown after the header line (Result block + artifacts), agent-facing
   *  "Use workflow_result(…)" hint stripped. */
  body: string
}

/** Parse the header + body from a completion message, or null if it doesn't
 *  match the expected shape (caller falls back to normal rendering). */
export function parseWorkflowCompletion(content: string): ParsedCompletion | null {
  const m = WF_COMPLETION_RE.exec(content)
  if (!m) return null
  let body = content.slice(m[0].length).trim()
  // Drop the trailing agent-facing tool hint — it's noise for the reader.
  body = body.replace(/\n*Use workflow_result\([^\n]*$/s, '').trim()
  return { name: m[1], runId: m[2], status: m[3], body }
}
