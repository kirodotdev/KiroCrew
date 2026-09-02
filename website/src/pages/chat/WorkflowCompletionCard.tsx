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
import { memo, useId } from 'react'
import { Workflow, CheckCircle2, AlertCircle, ChevronDown } from 'lucide-react'
import { PanelRightSolid } from '../../components/icons/panels'
import { useAppDispatch } from '../../store'
import { openActivityToTab } from '../../store/chatSlice'
import { sanitizeLlmOutput } from '../../utils/sanitize'
import MarkdownRenderer from '../../components/MarkdownRenderer'
import type { ChatMessage } from '../../types'

import { i18nT } from '../../i18n/t'
import { useRowDisclosure } from './rowDisclosure'
import { useLanguageGeneration } from '../../i18n/useLanguageGeneration'
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
  onSessionOpen,
  sessions,
  activeSession,
  disclosureKey,
}: {
  message: ChatMessage
  onFileOpen?: (path: string, opts?: { line?: number; endLine?: number }) => void
  onFolderOpen?: (path: string) => void
  /** Session switching for a `/chat?sid=` link in the result body, same triple
   *  the assistant row passes. Omitted by hosts with no slot roster. */
  onSessionOpen?: (key: string) => void
  sessions?: ReadonlyMap<string, string>
  activeSession?: string
  disclosureKey?: string
}) {
  useLanguageGeneration() // memo() bails out of the provider-level repaint; subscribe directly
  const dispatch = useAppDispatch()
  const [expanded, setExpanded] = useRowDisclosure(disclosureKey, false)
  // Names the expanded body's scroll region after the headline. useId keeps it
  // unique when a transcript renders many cards.
  const headlineId = useId()
  const parsed = parseWorkflowCompletion(message.content || '')
  if (!parsed) return null

  const { name, runId, status, body } = parsed
  const ok = status === 'finished'
  const label = sanitizeLlmOutput(name.slice(0, 80))

  // Row geometry -- the px-4 gutter and the --mc-content-width clamp -- belongs to
  // the HOST row wrapper, never to this card. ChatPage wraps every renderMessage
  // result, and the shared registries wrap this card through ctx.row. Re-applying
  // it here nested one clamp inside another and inset the card by a second full
  // gutter, so it sat 20px right of every sibling row and 40px narrower.
  return (
    <div className="rounded-md bg-accent/10 ring-1 ring-inset forced-colors:border ring-accent/20 overflow-hidden">
      <div className="flex items-center gap-2 px-3 py-2">
        <span className="shrink-0">
          {ok ? (
            <CheckCircle2 size={15} className="text-green-500" />
          ) : (
            <AlertCircle size={15} className="text-danger" />
          )}
        </span>
        <Workflow size={12} className="text-accent/70 shrink-0" />
        <span id={headlineId} className="truncate text-[13px] leading-5 font-medium text-text-strong">{label}</span>
        <span
          className={`shrink-0 text-[10px] leading-4 px-1.5 py-0.5 rounded border ${
            ok
              ? 'bg-green-500/10 border-green-500/20 text-green-500'
              : 'bg-danger/10 border-danger/20 text-danger'
          }`}
        >
          {status}
        </span>
        <span className="text-[10px] leading-4 text-muted font-mono truncate hidden sm:inline">{runId}</span>
        <div className="ml-auto flex items-center gap-1 shrink-0">
          <button
            type="button"
            onClick={() => dispatch(openActivityToTab('workflows'))}
            title={i18nT('pages.chat.workflowCompletionCard.open_in_the_workflows_panel')}
            aria-label={i18nT('pages.chat.workflowCompletionCard.open_in_the_workflows_panel')}
            className="pi-morph flex items-center gap-1 text-[11px] leading-4 text-accent hover:text-accent-hover bg-transparent border-none cursor-pointer px-1.5 py-1 rounded hover:bg-accent/10 transition-colors"
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
              className="flex items-center gap-1 text-[11px] leading-4 text-muted hover:text-text bg-transparent border-none cursor-pointer px-1.5 py-1 rounded hover:bg-bg-hover transition-colors"
            >
              {expanded ? i18nT('pages.chat.workflowCompletionCard.hide_result') : i18nT('pages.chat.workflowCompletionCard.show_result')}
              <ChevronDown size={13} className={`transition-transform ${expanded ? 'rotate-180' : ''}`} />
            </button>
          )}
        </div>
      </div>
      {expanded && body && (
        // max-h + overflow-y-auto: the body is a workflow's full result — a
        // machine-composed injected payload of unbounded length — so a long run
        // renders taller than the viewport. The body scrolls internally past
        // 24rem, so its height cannot displace the transcript rows below.
        // overflow-x-hidden is explicit because a non-visible y-axis computes
        // x's `visible` to `auto`: without it this becomes a two-axis scroller.
        // Nothing legitimately overflows x — inline code breaks (index.css
        // word-break:break-all) and body text wraps (break-words).
        // tabIndex + region role: a scroll region with no focusable descendant
        // is unreachable to a keyboard, so the scroller must itself be
        // focusable, and the region name points at the card headline.
        // The focus ring is inset: the card root clips at its rounded border
        // (overflow-hidden), and the global :focus-visible outline is disabled
        // (index.css), so an outward ring or UA outline would be swallowed on
        // every edge that touches the root. ring-inset paints inside the box.
        <div
          className="px-3 pb-2 pt-1 border-t border-accent/10 max-h-[24rem] overflow-y-auto overflow-x-hidden focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent"
          data-testid="workflow-completion-body"
          role="region"
          aria-labelledby={headlineId}
          // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- WCAG 2.1.1: this max-h scroller's only descendant is rendered markdown with no guaranteed focusable node, so removing tabIndex makes an overflowing workflow result impossible to scroll by keyboard; role=region + aria-labelledby keep it announced as a named landmark, not a control
          tabIndex={0}
        >
          <MarkdownRenderer content={body} onFileOpen={onFileOpen} onFolderOpen={onFolderOpen} onSessionOpen={onSessionOpen} sessions={sessions} activeSession={activeSession} />
        </div>
      )}
    </div>
  )
})

export default WorkflowCompletionCard
