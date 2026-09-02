import { useState, useEffect, useRef, memo, type ReactNode } from 'react'
import { CheckCircle, Handshake, Ban, Wrench, AlertTriangle } from 'lucide-react'
import { sanitizeLlmOutput } from '../../utils/sanitize'
import { purposeFromToolArgs } from '../../utils/toolPurpose'
import { ToolInputText } from '../../components/ToolInputText'
import ErrorNotice from '../../components/ErrorNotice'
import { ApiError } from '../../api/client'
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
  /**
   * Meta for EVERY pending permission in this group. When batching (>1 pending
   * + a batch handler), the row previews all N commands one `<pre>` each so the
   * human sees every call "Approve all N" will resolve — closing the
   * approve-unseen gap where only `permissionMeta` (the newest) was shown. For
   * a single pending approval this is unused; the row previews `permissionMeta`.
   */
  permissionMetas?: Record<string, unknown>[]
  /** Number of pending permission messages in this group (shown as indicator when > 1). */
  pendingPermCount?: number
  /** Callback for approve/reject (and trust, only when `canTrust`) — same as PermissionMessage.onApprove.
   *  MUST return the request's promise: it feeds `submitDecision`'s rollback
   *  (rejection restores the buttons). No `void` arm, so a fire-and-forget
   *  handler — the shape behind #5524 — cannot compile here. */
  onApprove?: (decision: string) => Promise<unknown>
  /**
   * Batch resolver: apply one decision to EVERY unresolved permission in this
   * group in a single click (Req 4.1-4.4). Wired only by hosts that can resolve
   * each pending approval id (ChatEmbed, via ChatMessageList). Used in place of `onApprove` when
   * `pendingPermCount > 1`; when there is a single pending approval the row
   * keeps the id-scoped `onApprove` path unchanged. Like `onApprove`, MUST
   * return the settle promise so `submitDecision`'s rollback restores the
   * buttons if any resolve fails. Gate-denied (TOOL_DENY) calls never surface
   * as pending permissions (backend gate; locked by the T5-guard test), so no
   * client-side exclusion is needed here.
   */
  onApproveBatch?: (decision: string) => Promise<unknown>
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
const CollapsibleToolGroup = memo(function CollapsibleToolGroup({ count, autoExpand, disclosureKey, hasPermission, isRunning, children, permissionMeta, permissionMetas, pendingPermCount, onApprove, onApproveBatch, canTrust, onViewActivity, activityOpen }: CollapsibleToolGroupProps) {
  useLanguageGeneration() // memo() bails out of the provider-level repaint; subscribe directly
  const [expanded, setExpanded] = useRowDisclosure(disclosureKey, !!autoExpand)
  const userToggled = useRef(false)
  const buttonsRef = useRef<HTMLDivElement | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [localResolved, setLocalResolved] = useState<string | null>(null)
  const [failure, setFailure] = useState<{ terminal: boolean; message: string; attempted: string } | null>(null)
  const needsAttention = !!hasPermission && !localResolved

  useEffect(() => { if (!userToggled.current) setExpanded(!!autoExpand) }, [autoExpand, setExpanded])

  // Reset approval state when permission props change (new approval arrives)
  useEffect(() => {
    setLocalResolved(null)
    setSubmitting(false)
    setFailure(null)
  }, [hasPermission, pendingPermCount])

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

  // Per-call previews for batch mode: one row per pending call, so the row shows
  // EVERY command "Approve all N" will resolve — in FULL, not truncated (a
  // truncated preview could hide a destructive suffix on a command with a benign
  // prefix at the human-vetting boundary). Each <pre> is height-bounded +
  // scrollable (below), so the full text never breaks layout. A meta with no
  // derivable command is NOT dropped — it renders a placeholder row (text: '')
  // so the rendered row count always equals pendingPermCount; dropping it would
  // let "Review all N" promise more rows than it shows and let a preview-less
  // call be approved sight-unseen (the exact gap this fix closes).
  const batchPreviews: string[] = needsAttention && permissionMetas && permissionMetas.length > 1
    ? permissionMetas.map(m => sanitizeLlmOutput(extractPreview(m)))
    : []
  // The full list is rendered inside a bounded, scrollable container (below), so
  // EVERY pending command stays reachable — no call is hidden from "Approve all
  // N" — while the Approve/Reject row stays above the fold on a large fan-out.

  // Dispatch an approval decision, optimistically reflecting it locally and rolling
  // back on failure. Logs failures for diagnostics via the error console.
  // When more than one approval is pending in this group AND the host supplied a
  // batch resolver, one click applies the decision to every pending call (Req 4.1-4.4);
  // otherwise the id-scoped single-approval path is used unchanged.
  const isBatch = !!onApproveBatch && !!pendingPermCount && pendingPermCount > 1
  const submitDecision = (decision: string) => {
    setFailure(null)
    setSubmitting(true)
    setLocalResolved(decision)
    // Batch only approve/reject. 'trust' records a STANDING grant and is never
    // batched: the trust tier is fail-closed (#5400/#5434) and must stay
    // id-scoped to the single owned id, so it always routes through onApprove.
    const batchThis = isBatch && decision !== 'trust'
    void Promise.resolve()
      .then(() => (batchThis ? onApproveBatch!(decision) : onApprove?.(decision)))
      .catch((err) => {
        // Intentional error diagnostic: surfaces a failed approval round-trip.
        // eslint-disable-next-line no-console
        console.error('Approval failed:', err)
        setLocalResolved(null)
        setSubmitting(false)
        const refusal = err instanceof ApiError ? err : null
        const gone = !!refusal && !refusal.authRequired
          && (refusal.status === 404 || (refusal.status === 400 && refusal.message === 'no pending approval'))
        setFailure({
          terminal: gone,
          message: refusal?.message ?? '',
          attempted: decision,
        })
      })
  }

  // Optimistic resolution removes the focused button. On a retryable failure,
  // return focus to the exact attempted decision so a keyboard retry cannot
  // silently choose a different verdict. Terminal refusals have no live action
  // to restore: the approval is already gone.
  useEffect(() => {
    if (!failure || failure.terminal) return
    const buttons = Array.from(buttonsRef.current?.querySelectorAll('button') ?? [])
    if (!buttons.length) return
    const target = failure.attempted === 'approved' ? buttons[0]
      : failure.attempted === 'rejected' ? buttons[buttons.length - 1]
        : buttons.length > 2 ? buttons[1] : buttons[0]
    target.focus()
  }, [failure])

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
      {needsAttention && (onApprove || onApproveBatch) && (isBatch ? batchPreviews.length > 0 : !!truncated) && (
        <div className="mt-1 ml-4 pl-3 shadow-[inset_2px_0_0_0_theme(colors.amber.400)] forced-colors:border-l-2">
          {isBatch ? (
            <>
              {/* Batch: preview EVERY pending call so "Approve all N" is not a
                  blind approval — the human sees each command being resolved. */}
              <div className="text-[12px] leading-5 text-muted mb-1">{i18nT('pages.chat.collapsibleToolGroup.batch_preview_all', { count: pendingPermCount })}</div>
              {/* Every pending command renders; the container is height-bounded and
                  scrolls, so nothing is hidden AND the buttons stay above the fold
                  even on a large fan-out. */}
              <div className="max-h-[15em] overflow-y-auto mb-2">
                {batchPreviews.map((p, i) => (
                  p
                    ? <pre key={i} className="bg-bg-hover rounded-md px-3 py-2 text-[13px] leading-5 font-mono overflow-x-auto whitespace-pre-wrap break-all max-h-[4.5em] overflow-y-auto mb-2 last:mb-0"><ToolInputText text={p} /></pre>
                    : <div key={i} className="bg-bg-hover rounded-md px-3 py-2 text-[13px] leading-5 italic text-muted mb-2 last:mb-0">{i18nT('pages.chat.collapsibleToolGroup.batch_preview_none')}</div>
                ))}
              </div>
            </>
          ) : (
            <pre className="bg-bg-hover rounded-md px-3 py-2 text-[13px] leading-5 font-mono overflow-x-auto whitespace-pre-wrap break-all max-h-[4.5em] overflow-y-auto mb-2"><ToolInputText text={truncated} /></pre>
          )}
        </div>
      )}
      {needsAttention && (onApprove || onApproveBatch) && !failure?.terminal && (
        <div ref={buttonsRef} className="mt-1 ml-4 pl-3 flex gap-2 flex-wrap">
          <button disabled={submitting} className="px-3 py-1 rounded-md border border-border bg-transparent text-muted text-[13px] leading-5 cursor-pointer font-body hover:text-text hover:border-border-strong hover:bg-bg-hover transition-all disabled:opacity-50 disabled:cursor-not-allowed" onClick={e => { e.stopPropagation(); submitDecision('approved') }}><CheckCircle className="lucide-inline" /> {isBatch ? i18nT('pages.chat.collapsibleToolGroup.approve_all', { count: pendingPermCount }) : i18nT('pages.chat.collapsibleToolGroup.approve')}</button>
          {canTrust && !isBatch && <button disabled={submitting} className="px-3 py-1 rounded-md border border-border bg-transparent text-muted text-[13px] leading-5 cursor-pointer font-body hover:text-text hover:border-border-strong hover:bg-bg-hover transition-all disabled:opacity-50 disabled:cursor-not-allowed" onClick={e => { e.stopPropagation(); submitDecision('trust') }}><Handshake className="lucide-inline" /> {i18nT('pages.chat.collapsibleToolGroup.trust')}</button>}
          <button disabled={submitting} className="px-3 py-1 rounded-md border border-border bg-transparent text-muted text-[13px] leading-5 cursor-pointer font-body hover:text-danger hover:border-danger transition-all disabled:opacity-50 disabled:cursor-not-allowed" onClick={e => { e.stopPropagation(); submitDecision('rejected') }}><Ban className="lucide-inline" /> {isBatch ? i18nT('pages.chat.collapsibleToolGroup.reject_all', { count: pendingPermCount }) : i18nT('pages.chat.collapsibleToolGroup.reject')}</button>
        </div>
      )}

      {failure !== null && (
        <ErrorNotice variant="inline" className="mt-1 ml-4 pl-3" message={failure.terminal
          ? i18nT('components.approvalCard.approval_no_longer_pending')
          : failure.message
            ? i18nT('components.approvalCard.decision_not_recorded_error', { error: failure.message })
            : i18nT('components.approvalCard.decision_failed')} />
      )}

      {expanded && <div className="mt-1 ml-4 pl-3 shadow-[inset_2px_0_0_0_var(--border)] forced-colors:border-l-2 flex flex-col gap-1">
        {children}
        {onViewActivity && !activityOpen && <button className="text-[12px] leading-5 text-accent hover:underline cursor-pointer font-body self-start mt-1" onClick={onViewActivity}>{i18nT('pages.chat.collapsibleToolGroup.view_full_activity')}</button>}
      </div>}
    </div>
  )
})

export default CollapsibleToolGroup
