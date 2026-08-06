/**
 * WorkflowCompletionCard — renders a workflow completion event compactly.
 *
 * When a background workflow finishes, the backend injects an `assistant`
 * message into the originating chat (see dashboard/workflow_inject.py) whose
 * markdown begins with "[Workflow completion event]" and embeds the full result
 * JSON. Rendered as plain markdown that is a wall of JSON. This card replaces it
 * with a compact status header (✓/✗ + name + run id) that opens the Workflows
 * side panel, and folds the full result markdown behind a "Show result" toggle.
 *
 * Render-only: the underlying message content is untouched, so the launching
 * agent still receives the complete result as context. Detection is content-
 * based (not the WS `kind`, which is dropped when the message is persisted), so
 * it works live and when a conversation is reloaded from history.
 */
import { memo } from 'react'
import { Workflow, CheckCircle2, AlertCircle, ChevronDown } from 'lucide-react'
import { PanelRightSolid } from '../../components/icons/panels'
import { useAppDispatch } from '../../store'
import { openActivityToTab } from '../../store/chatSlice'
import { sanitizeLlmOutput } from '../../utils/sanitize'
import MarkdownRenderer from '../../components/MarkdownRenderer'
import type { ChatMessage } from '../../types'

import { i18nT } from '../../i18n/t'
import { useRowDisclosure } from './rowDisclosure'
const WF_COMPLETION_PREFIX = '[Workflow completion event]'
// Name is backtick-delimited; allow any char except a backtick (including
// newlines) so an unusual name doesn't make the header fail to parse. If it
// still doesn't match (e.g. a name containing a backtick), detection falls back
// to normal rendering rather than dropping the message — see
// isWorkflowCompletionMessage.
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

const WorkflowCompletionCard = memo(function WorkflowCompletionCard({
  message,
  onFileOpen,
  onFolderOpen,
  disclosureKey,
}: {
  message: ChatMessage
  onFileOpen?: (path: string, opts?: { line?: number; endLine?: number }) => void
  onFolderOpen?: (path: string) => void
  disclosureKey?: string
}) {
  const dispatch = useAppDispatch()
  const [expanded, setExpanded] = useRowDisclosure(disclosureKey, false)
  const parsed = parseWorkflowCompletion(message.content || '')
  if (!parsed) return null

  const { name, runId, status, body } = parsed
  const ok = status === 'finished'
  const label = sanitizeLlmOutput(name.slice(0, 80))

  return (
    <div className="px-5 mx-auto w-full py-0.5" style={{ maxWidth: 'var(--mc-content-width, 900px)' }}>
      <div className="rounded-md bg-accent/10 border border-accent/20 overflow-hidden">
        <div className="flex items-center gap-2 px-3 py-2">
          <span className="shrink-0">
            {ok ? (
              <CheckCircle2 size={15} className="text-green-500" />
            ) : (
              <AlertCircle size={15} className="text-danger" />
            )}
          </span>
          <Workflow size={12} className="text-accent/70 shrink-0" />
          <span className="truncate text-[13px] font-medium text-text-strong">{label}</span>
          <span
            className={`shrink-0 text-[10px] px-1.5 py-0.5 rounded border ${
              ok
                ? 'bg-green-500/10 border-green-500/20 text-green-500'
                : 'bg-danger/10 border-danger/20 text-danger'
            }`}
          >
            {status}
          </span>
          <span className="text-[10px] text-muted font-mono truncate hidden sm:inline">{runId}</span>
          <div className="ml-auto flex items-center gap-1 shrink-0">
            <button
              type="button"
              onClick={() => dispatch(openActivityToTab('workflows'))}
              title={i18nT('pages.chat.workflowCompletionCard.open_in_the_workflows_panel')}
              aria-label={i18nT('pages.chat.workflowCompletionCard.open_in_the_workflows_panel')}
              className="pi-morph flex items-center gap-1 text-[11px] text-accent hover:text-accent-hover bg-transparent border-none cursor-pointer px-1.5 py-1 rounded hover:bg-accent/10 transition-colors"
            >
              <PanelRightSolid size={13} />
              <span className="hidden sm:inline">{i18nT('pages.chat.workflowCompletionCard.panel')}</span>
            </button>
            {body && (
              <button
                type="button"
                onClick={() => setExpanded(e => !e)}
                aria-expanded={expanded}
                title={expanded ? i18nT('pages.chat.workflowCompletionCard.hide_result') : i18nT('pages.chat.workflowCompletionCard.show_result')}
                className="flex items-center gap-1 text-[11px] text-muted hover:text-text bg-transparent border-none cursor-pointer px-1.5 py-1 rounded hover:bg-bg-hover transition-colors"
              >
                {expanded ? i18nT('pages.chat.workflowCompletionCard.hide_result') : i18nT('pages.chat.workflowCompletionCard.show_result')}
                <ChevronDown size={13} className={`transition-transform ${expanded ? 'rotate-180' : ''}`} />
              </button>
            )}
          </div>
        </div>
        {expanded && body && (
          <div className="px-3 pb-2 pt-1 border-t border-accent/10">
            <MarkdownRenderer content={body} onFileOpen={onFileOpen} onFolderOpen={onFolderOpen} />
          </div>
        )}
      </div>
    </div>
  )
})

export default WorkflowCompletionCard
