import { AnimatePresence, motion } from 'framer-motion'
import { Ban, Bot, CheckCircle, Loader2, Target } from 'lucide-react'
import { Glass } from '../Glass'
import type { SubagentActivity } from '../../types'
import { i18nT } from '../../i18n/t'
import { approvalBtnClass, spawnGoneKey } from './approval'
import ErrorNotice from '../ErrorNotice'
import { refusedNotice } from '../notifications/notifMeta'
/**
 * Sub-agent spawn-approval banner — a top-level signal that one or more
 * sub-agents are queued awaiting the user's approval to run, with inline
 * Approve/Reject so the decision can be made without leaving the
 * composer. Single pending → a compact one-line row. Multiple → header
 * Approve all / Reject all plus a per-agent row (task + Approve/Reject)
 * so one can run while another is rejected. "Review in panel" opens the
 * Subagents tab. Not a single <button> wrapper — every control is its
 * own button. Plain glass, not the warn tint the tool-approval pane
 * below wears: when both are up, two warn panes in one band read as ONE
 * request (UX review of 76851c90 -- "I'd fear double-approving"), and
 * this card's Bot framing and pulse already say what it is.
 * While the tool-approval bar below is ALSO pending, this card keeps its
 * count and "Review in panel" but withholds Approve/Reject and its glow:
 * one set of decision buttons on screen at a time, so a reader cannot
 * take the two panes for one request and wonder whether a click answers
 * half of it (UX review of 21b8e79b). The buttons return the moment the
 * tool decision lands; the Subagents tab can resolve the spawn meanwhile.
 */
export function SpawnApprovalCard({ pendingSpawnApprovals, hasApproval, spawnApprovalsResolving, resolveOneSpawn, resolveSpawnApprovals, reviewSpawnApprovals, goneSpawnKeys = [], spawnApprovalError = null }: {
  pendingSpawnApprovals: SubagentActivity[]
  hasApproval: boolean
  spawnApprovalsResolving: boolean
  resolveOneSpawn: (a: SubagentActivity, action: 'approve' | 'reject') => void
  resolveSpawnApprovals: (action: 'approve' | 'reject') => void
  reviewSpawnApprovals: () => void
  /** Spawns known to be gone (by `spawnGoneKey`): their Approve/Reject are
   *  withdrawn and the spawn itself says why. */
  goneSpawnKeys?: readonly string[]
  /** Why the last press did not land when it may still succeed, rendered
   *  under the banner. */
  spawnApprovalError?: string | null
}) {
  const isGone = (a: SubagentActivity) => goneSpawnKeys.includes(spawnGoneKey(a))
  // The headline and the bulk labels count only spawns still awaiting a
  // decision, so a gone spawn is never announced as awaiting approval.
  const decidableCount = pendingSpawnApprovals.filter(a => !isGone(a)).length
  const anyDecidable = decidableCount > 0
  // A gone spawn is only ever learned from the reader's own press, so it
  // reads the press-failed sentence every other surface uses for it.
  const goneText = refusedNotice()
  return (
    <AnimatePresence>
      {pendingSpawnApprovals.length > 0 && (
        <motion.div
          initial={{ opacity: 0, y: 8 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y: 8 }}
          transition={{ type: 'spring', damping: 25, stiffness: 300, mass: 0.8 }}
        >
          <Glass variant="chip" radius={16} className={`w-full mb-2${hasApproval || !anyDecidable ? '' : ' approval-glow'}`} data-testid="spawn-approval-card">
            {/* With nothing awaiting a decision the whole row sits on the page's
             *  solid surface: the notice, the names and Review in panel all keep
             *  their normal contrast instead of reading as greyed out on the
             *  frosted chip (UX review of c18b2d3f0d / 965720d7c0). */}
            <div className={`flex items-center gap-1.5 px-3.5 py-2.5 select-none flex-wrap${anyDecidable ? '' : ' bg-bg rounded-[16px]'}`} data-settled={anyDecidable ? undefined : true}>
              <Bot size={13} className="text-warn shrink-0" />
              {/* No hand-off on these notices: this banner sits on the composer,
                  whose unsent draft the hand-off's navigation would discard. */}
              {anyDecidable ? (
              <span className="text-[13px] font-body text-muted flex-1 min-w-0">
                {/* While the tool approval bar is up, the decision lives THERE
                 *  (the spawn's own permission row is what holds the bar), so
                 *  this line must not point at itself as the thing to approve:
                 *  it names the count and defers to the panel link. */}
                {hasApproval
                  ? i18nT('components.chatInput.spawn_pending', { count: decidableCount })
                  : i18nT('components.chatInput.spawn_awaiting', { count: decidableCount })}
              </span>
              ) : (
                // Every spawn here is gone: no count is announced, and each
                // spawn's own row below says why.
                <span className="flex-1 min-w-0" />
              )}
              {/* The action area swaps between three forms (resolving / panel
               *  link only / Approve + Reject) as the tool bar comes and goes;
               *  `mode="wait"` fades one out before the next fades in, so the
               *  swap reads as the same slot changing state, not a new control
               *  appearing from nowhere. */}
              <AnimatePresence mode="wait" initial={false}>
              {spawnApprovalsResolving ? (
                <motion.span key="resolving" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} transition={{ duration: 0.15 }} className="inline-flex items-center gap-1 text-[12px] text-muted/60 shrink-0">
                  <Loader2 size={12} className="animate-spin shrink-0" />{i18nT('components.chatInput.resolving')}
                </motion.span>
              ) : hasApproval || !anyDecidable ? (
                <motion.button
                  key="panel-only"
                  initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} transition={{ duration: 0.15 }}
                  type="button"
                  onClick={reviewSpawnApprovals}
                  className="inline-flex items-center gap-1 text-[11px] text-muted hover:text-text shrink-0 cursor-pointer bg-transparent border-none px-1"
                >
                  <Target size={11} className="shrink-0" />{i18nT('components.chatInput.review_in_panel')}
                </motion.button>
              ) : (
                <motion.div key="decide" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} transition={{ duration: 0.15 }} className="flex items-center gap-1.5 shrink-0">
                  <button
                    type="button"
                    onClick={() => resolveSpawnApprovals('approve')}
                    className={approvalBtnClass}
                  >
                    <CheckCircle size={12} className="shrink-0" />
                    {decidableCount === 1 ? i18nT('components.chatInput.approve') : i18nT('components.chatInput.approve_all')}
                  </button>
                  <button
                    type="button"
                    onClick={() => resolveSpawnApprovals('reject')}
                    className={`${approvalBtnClass} hover:!text-danger hover:!border-danger`}
                  >
                    <Ban size={12} className="shrink-0" />
                    {decidableCount === 1 ? i18nT('components.chatInput.reject') : i18nT('components.chatInput.reject_all')}
                  </button>
                  <button
                    type="button"
                    onClick={reviewSpawnApprovals}
                    className="inline-flex items-center gap-1 text-[11px] text-muted hover:text-text shrink-0 cursor-pointer bg-transparent border-none px-1"
                  >
                    <Target size={11} className="shrink-0" />{i18nT('components.chatInput.review_in_panel')}
                  </button>
                </motion.div>
              )}
              </AnimatePresence>
            </div>
            {/* Per-agent rows — only when more than one is pending, so a single
             *  spawn stays a compact one-liner. Each row resolves just its own
             *  sub-agent via resolveOneSpawn. They collapse out when a tool
             *  approval lands, the same way the action area fades: the card
             *  shrinks to its one-line form instead of the rows vanishing on
             *  one frame while the header cross-fades (UX review of fddfcb86). */}
            <AnimatePresence initial={false}>
            {((pendingSpawnApprovals.length > 1 && !hasApproval) || !anyDecidable) && (
              <motion.div key="rows" initial={{ opacity: 0, height: 0 }} animate={{ opacity: 1, height: 'auto' }} exit={{ opacity: 0, height: 0 }} transition={{ duration: 0.15 }} className="overflow-hidden">
              <div className="px-3.5 pb-2.5 flex flex-col gap-1.5">
                {pendingSpawnApprovals.map(a => (
                  <div key={a.id} data-gone={isGone(a) || undefined} className={`flex items-center gap-2 rounded-lg border border-border/60 px-2.5 py-1.5 ${isGone(a) ? 'bg-bg flex-wrap' : 'bg-bg/40'}`}>
                    {/* A gone row is settled: its name is struck through, so the
                     *  header's count of spawns awaiting approval matches the
                     *  rows that still offer a decision. */}
                    <code className={`text-[11px] font-mono flex-1 min-w-0 truncate ${isGone(a) ? 'text-muted/60 line-through' : 'text-muted/80'}`} title={a.task || a.agent || a.id}>
                      {a.task || a.agent || a.id}
                    </code>
                    {isGone(a) ? (
                      // Its own line under the name, at the row's full width.
                      <div className="basis-full">
                        {/* No hand-off: this banner sits on the composer, whose
                         *  unsent draft the hand-off's navigation would discard. */}
                        <ErrorNotice variant="inline" testId="spawn-approval-gone-row" message={goneText} />
                      </div>
                    ) : a.approving ? (
                      <span className="inline-flex items-center gap-1 text-[11px] text-muted/60 shrink-0">
                        <Loader2 size={11} className="animate-spin shrink-0" />{i18nT('components.chatInput.resolving')}
                      </span>
                    ) : (
                      <div className="flex items-center gap-1 shrink-0">
                        <button
                          type="button"
                          aria-label={i18nT('components.chatInput.approve_sub_agent', { name: a.task || a.agent || a.id })}
                          onClick={() => resolveOneSpawn(a, 'approve')}
                          className={approvalBtnClass}
                        >
                          <CheckCircle size={12} className="shrink-0" />{i18nT('components.chatInput.approve')}
                        </button>
                        <button
                          type="button"
                          aria-label={i18nT('components.chatInput.reject_sub_agent', { name: a.task || a.agent || a.id })}
                          onClick={() => resolveOneSpawn(a, 'reject')}
                          className={`${approvalBtnClass} hover:!text-danger hover:!border-danger`}
                        >
                          <Ban size={12} className="shrink-0" />{i18nT('components.chatInput.reject')}
                        </button>
                      </div>
                    )}
                  </div>
                ))}
              </div>
              </motion.div>
            )}
            </AnimatePresence>
            {/* No hand-off: this banner sits on the composer, whose unsent
                draft the hand-off's navigation to a new chat would discard. */}
            {spawnApprovalError && anyDecidable && (
              <div className="px-3.5 pb-2.5">
                <ErrorNotice variant="inline" testId="spawn-approval-error" message={spawnApprovalError} />
              </div>
            )}
          </Glass>
        </motion.div>
      )}
    </AnimatePresence>
  )
}
