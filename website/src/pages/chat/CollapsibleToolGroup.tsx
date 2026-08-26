import { useState, useEffect, useRef, memo, type ReactNode } from 'react'
import { CheckCircle, Handshake, Ban, Wrench, AlertTriangle } from 'lucide-react'
import { sanitizeLlmOutput } from '../../utils/sanitize'
import { purposeFromToolArgs } from '../../utils/toolPurpose'
import { ToolInputText } from '../../components/ToolInputText'
import { useRowDisclosure } from './rowDisclosure'

import { i18nT } from '../../i18n/t'
import { useLanguageGeneration } from '../../i18n/useLanguageGeneration'
interface CollapsibleToolGroupProps {
  count: number
  autoExpand?: boolean
  disclosureKey?: string
  hasPermission?: boolean
  isRunning?: boolean
  children: ReactNode
  /** Permission message meta — used to extract command preview when approval pending. */
  permissionMeta?: Record<string, unknown>
  /** Number of pending permission messages in this group (shown as indicator when > 1). */
  pendingPermCount?: number
  /** Callback for approve/reject (and trust, only when `canTrust`) — same as PermissionMessage.onApprove.
   *  MUST return the request's promise: it feeds `submitDecision`'s rollback
   *  (rejection restores the buttons). No `void` arm, so a fire-and-forget
   *  handler — the shape behind #5524 — cannot compile here. */
  onApprove?: (decision: string) => Promise<unknown>
  /**
   * Offer the standing-trust tier. FAIL-CLOSED: leave unset unless this mount's
   * `onApprove` routes to an endpoint that actually RECORDS standing trust
   * (POST /api/chat/slots/{slot}/approve carries the decision verbatim).
   * The common resolve path — ChatPage's `toApiDecision` into the one-shot
   * `api.resolveApproval` — has no trust verb, so offering Trust there (or
   * labelling a decision "Trusted") overstates the grant: the next identical
   * call prompts again (#5400 on the spawn card, #5434 on this row).
   */
  canTrust?: boolean
  /** Callback to open the Activity Viewer. */
  onViewActivity?: () => void
  /** Whether the Activity Viewer is currently open. */
  activityOpen?: boolean
}

/** Extract a human-readable command preview from permission meta. */
function extractPreview(meta?: Record<string, unknown>): string {
  if (!meta) return ''
  const ti = meta.tool_input
  if (typeof ti === 'string') return ti
  if (ti && typeof ti === 'object') {
    const obj = ti as Record<string, unknown>
    if (typeof obj.command === 'string') return obj.command
    // Pretty-print (2-space indent) so nested structure renders as real line
    // breaks in the <pre whitespace-pre-wrap> card, instead of a single
    // unreadable line with escaped \n / \t sequences.
    return JSON.stringify(ti, null, 2)
  }
  // Last resort: the agent-authored purpose line, read by shape so a
  // paraphrased key spelling still previews (see utils/toolPurpose).
  return purposeFromToolArgs(meta)
}

/** Collapsible row that wraps tool/thinking/permission messages — always collapsed unless autoExpand. */
const CollapsibleToolGroup = memo(function CollapsibleToolGroup({ count, autoExpand, disclosureKey, hasPermission, isRunning, children, permissionMeta, pendingPermCount, onApprove, canTrust, onViewActivity, activityOpen }: CollapsibleToolGroupProps) {
  useLanguageGeneration() // memo() bails out of the provider-level repaint; subscribe directly
  const [expanded, setExpanded] = useRowDisclosure(disclosureKey, !!autoExpand)
  const userToggled = useRef(false)
  const [submitting, setSubmitting] = useState(false)
  const [localResolved, setLocalResolved] = useState<string | null>(null)
  const needsAttention = !!hasPermission && !localResolved

  useEffect(() => { if (!userToggled.current) setExpanded(!!autoExpand) }, [autoExpand, setExpanded])

  // Reset approval state when permission props change (new approval arrives)
  useEffect(() => { setLocalResolved(null); setSubmitting(false) }, [hasPermission, pendingPermCount])

  // Auto-collapse when tools finish running (unless user manually toggled)
  const wasRunning = useRef(false)
  useEffect(() => {
    if (wasRunning.current && !isRunning && !userToggled.current) setExpanded(false)
    wasRunning.current = !!isRunning
  }, [isRunning, setExpanded])

  // The 'trust' entries are reachable only from a `canTrust` mount (see the
  // prop's contract above): a mount resolving through the one-shot
  // `api.resolveApproval` endpoint never offers the Trust button, so it can
  // never wear a "Trusted" label it did not earn (#5400, #5434).
  const decisionLabel: Record<string, ReactNode> = { approved: <><CheckCircle className="lucide-inline" /> {i18nT('pages.chat.collapsibleToolGroup.approved')}</>, trust: <><Handshake className="lucide-inline" /> {i18nT('pages.chat.collapsibleToolGroup.trusted')}</>, rejected: <><Ban className="lucide-inline" /> {i18nT('pages.chat.collapsibleToolGroup.rejected')}</> }
  const labelNode = localResolved
    ? (decisionLabel[localResolved] || <><CheckCircle className="lucide-inline" /> {i18nT('pages.chat.collapsibleToolGroup.resolved')}</>)
    : needsAttention
      ? (pendingPermCount && pendingPermCount > 1 ? <><AlertTriangle className="lucide-inline" /> {pendingPermCount} {i18nT('pages.chat.collapsibleToolGroup.approvals_pending')}</> : <><AlertTriangle className="lucide-inline" /> {i18nT('pages.chat.collapsibleToolGroup.approval_needed')}</>)
      : isRunning
        ? <><Wrench className="lucide-inline" /> {i18nT('pages.chat.collapsibleToolGroup.running_tools')}</>
        : <><Wrench className="lucide-inline" /> {i18nT('pages.chat.collapsibleToolGroup.tool_call', { count: count })}</>
  const labelText = localResolved
    ? (localResolved === 'approved' ? i18nT('pages.chat.collapsibleToolGroup.approved') : localResolved === 'trust' ? i18nT('pages.chat.collapsibleToolGroup.trusted') : i18nT('pages.chat.collapsibleToolGroup.rejected'))
    : needsAttention ? i18nT('pages.chat.collapsibleToolGroup.approval_needed') : isRunning ? i18nT('pages.chat.collapsibleToolGroup.running_tools') : i18nT('pages.chat.collapsibleToolGroup.tool_call', { count: count })

  const preview = needsAttention ? sanitizeLlmOutput(extractPreview(permissionMeta)) : ''
  const truncated = preview.length > 150 ? preview.slice(0, 150) + '…' : preview

  // Dispatch an approval decision, optimistically reflecting it locally and rolling
  // back on failure. Logs failures for diagnostics via the error console.
  const submitDecision = (decision: string) => {
    setSubmitting(true)
    setLocalResolved(decision)
    void Promise.resolve()
      .then(() => onApprove?.(decision))
      .catch((err) => {
        // Intentional error diagnostic: surfaces a failed approval round-trip.
        // eslint-disable-next-line no-console
        console.error('Approval failed:', err)
        setLocalResolved(null)
        setSubmitting(false)
      })
  }

  return (
    <div className="my-1">
      <button
        className={`flex items-center gap-2 px-4 py-2 rounded-md text-[13px] leading-5 font-mono text-muted bg-card ring-1 ring-inset forced-colors:border cursor-pointer transition-all w-full text-left ${needsAttention ? 'ring-amber-400 hover:ring-amber-300' : localResolved ? 'ring-ok/60 hover:ring-ok/80' : 'ring-border hover:ring-border-strong'} hover:text-text`}
        onClick={() => { userToggled.current = true; setExpanded(e => !e) }}
        aria-expanded={expanded}
        aria-label={`${expanded ? i18nT('pages.chat.collapsibleToolGroup.collapse') : i18nT('pages.chat.collapsibleToolGroup.expand')} ${labelText}`}
      >
        {needsAttention ? (
          <span className="relative w-2.5 h-2.5 flex-shrink-0" aria-label={i18nT('pages.chat.collapsibleToolGroup.approval_needed')}>
            <span className="absolute inset-0 rounded-full bg-amber-400 animate-ping opacity-60" />
            <span className="relative block w-2.5 h-2.5 rounded-full bg-amber-400" />
          </span>
        ) : localResolved ? (
          <span className="w-2.5 h-2.5 rounded-full bg-ok flex-shrink-0" aria-label={i18nT('pages.chat.collapsibleToolGroup.resolved')} />
        ) : isRunning ? (
          <span className="w-2.5 h-2.5 rounded-full bg-green-400 animate-pulse flex-shrink-0" aria-label={i18nT('pages.chat.collapsibleToolGroup.running')} />
        ) : (
          <span className={`transition-transform duration-150 ${expanded ? 'rotate-90' : ''}`}>▶</span>
        )}
        <span>{labelNode}</span>
      </button>

      {/* Inline approval: command preview + action buttons. Rendered in BOTH
          disclosure states: a pending group auto-expands while the agent is
          running (ChatMessageList sets autoExpand on recent running groups),
          and grouped permission messages render null inside the children, so
          gating this row on !expanded left the expanded pending group with no
          actionable buttons — a dead end exactly while the agent is parked
          waiting on the user (#5487). */}
      {needsAttention && onApprove && truncated && (
        <div className="mt-1 ml-4 pl-3 shadow-[inset_2px_0_0_0_theme(colors.amber.400)] forced-colors:border-l-2">
          <pre className="bg-bg-hover rounded-md px-3 py-2 text-[13px] leading-5 font-mono overflow-x-auto whitespace-pre-wrap break-all max-h-[4.5em] overflow-y-auto mb-2"><ToolInputText text={truncated} /></pre>
        </div>
      )}
      {needsAttention && onApprove && (
        <div className="mt-1 ml-4 pl-3 flex gap-2 flex-wrap">
          <button disabled={submitting} className="px-3 py-1 rounded-md border border-border bg-transparent text-muted text-[13px] leading-5 cursor-pointer font-body hover:text-text hover:border-border-strong hover:bg-bg-hover transition-all disabled:opacity-50 disabled:cursor-not-allowed" onClick={e => { e.stopPropagation(); submitDecision('approved') }}><CheckCircle className="lucide-inline" /> {i18nT('pages.chat.collapsibleToolGroup.approve')}</button>
          {canTrust && <button disabled={submitting} className="px-3 py-1 rounded-md border border-border bg-transparent text-muted text-[13px] leading-5 cursor-pointer font-body hover:text-text hover:border-border-strong hover:bg-bg-hover transition-all disabled:opacity-50 disabled:cursor-not-allowed" onClick={e => { e.stopPropagation(); submitDecision('trust') }}><Handshake className="lucide-inline" /> {i18nT('pages.chat.collapsibleToolGroup.trust')}</button>}
          <button disabled={submitting} className="px-3 py-1 rounded-md border border-border bg-transparent text-muted text-[13px] leading-5 cursor-pointer font-body hover:text-danger hover:border-danger transition-all disabled:opacity-50 disabled:cursor-not-allowed" onClick={e => { e.stopPropagation(); submitDecision('rejected') }}><Ban className="lucide-inline" /> {i18nT('pages.chat.collapsibleToolGroup.reject')}</button>
        </div>
      )}

      {expanded && <div className="mt-1 ml-4 pl-3 shadow-[inset_2px_0_0_0_var(--border)] forced-colors:border-l-2 flex flex-col gap-1">
        {children}
        {onViewActivity && !activityOpen && <button className="text-[12px] leading-5 text-accent hover:underline cursor-pointer font-body self-start mt-1" onClick={onViewActivity}>{i18nT('pages.chat.collapsibleToolGroup.view_full_activity')}</button>}
      </div>}
    </div>
  )
})

export default CollapsibleToolGroup
