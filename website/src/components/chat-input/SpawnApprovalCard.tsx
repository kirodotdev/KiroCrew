import { AnimatePresence, motion } from 'framer-motion'
import { Ban, Bot, CheckCircle, Loader2, Target } from 'lucide-react'
import { Glass } from '../Glass'
import type { SubagentActivity } from '../../types'
import { i18nT } from '../../i18n/t'
import { approvalBtnClass } from './approval'
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
export function SpawnApprovalCard({ pendingSpawnApprovals, hasApproval, spawnApprovalsResolving, resolveOneSpawn, resolveSpawnApprovals, reviewSpawnApprovals }: {
  pendingSpawnApprovals: SubagentActivity[]
  hasApproval: boolean
  spawnApprovalsResolving: boolean
  resolveOneSpawn: (a: SubagentActivity, action: 'approve' | 'reject') => void
  resolveSpawnApprovals: (action: 'approve' | 'reject') => void
  reviewSpawnApprovals: () => void
}) {
  return (
    <AnimatePresence>
      {pendingSpawnApprovals.length > 0 && (
        <motion.div
          initial={{ opacity: 0, y: 8 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y: 8 }}
          transition={{ type: 'spring', damping: 25, stiffness: 300, mass: 0.8 }}
        >
          <Glass variant="chip" radius={16} className={`w-full mb-2${hasApproval ? '' : ' approval-glow'}`} data-testid="spawn-approval-card">
            <div className="flex items-center gap-1.5 px-3.5 py-2.5 select-none flex-wrap">
              <Bot size={13} className="text-warn shrink-0" />
              <span className="text-[13px] font-body text-muted flex-1 min-w-0">
                {/* While the tool approval bar is up, the decision lives THERE
                 *  (the spawn's own permission row is what holds the bar), so
                 *  this line must not point at itself as the thing to approve:
                 *  it names the count and defers to the panel link. */}
                {hasApproval
                  ? i18nT('components.chatInput.spawn_pending', { count: pendingSpawnApprovals.length })
                  : i18nT('components.chatInput.spawn_awaiting', { count: pendingSpawnApprovals.length })}
              </span>
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
              ) : hasApproval ? (
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
                    {pendingSpawnApprovals.length === 1 ? i18nT('components.chatInput.approve') : i18nT('components.chatInput.approve_all')}
                  </button>
                  <button
                    type="button"
                    onClick={() => resolveSpawnApprovals('reject')}
                    className={`${approvalBtnClass} hover:!text-danger hover:!border-danger`}
                  >
                    <Ban size={12} className="shrink-0" />
                    {pendingSpawnApprovals.length === 1 ? i18nT('components.chatInput.reject') : i18nT('components.chatInput.reject_all')}
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
            {pendingSpawnApprovals.length > 1 && !hasApproval && (
              <motion.div key="rows" initial={{ opacity: 0, height: 0 }} animate={{ opacity: 1, height: 'auto' }} exit={{ opacity: 0, height: 0 }} transition={{ duration: 0.15 }} className="overflow-hidden">
              <div className="px-3.5 pb-2.5 flex flex-col gap-1.5">
                {pendingSpawnApprovals.map(a => (
                  <div key={a.id} className="flex items-center gap-2 rounded-lg border border-border/60 bg-bg/40 px-2.5 py-1.5">
                    <code className="text-[11px] font-mono text-muted/80 flex-1 min-w-0 truncate" title={a.task || a.agent || a.id}>
                      {a.task || a.agent || a.id}
                    </code>
                    {a.approving ? (
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
          </Glass>
        </motion.div>
      )}
    </AnimatePresence>
  )
}
