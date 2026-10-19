import type { RootState } from '../store'
import {
  selectSidebarStartedSubagentCounts,
  selectSidebarApprovalCounts,
  selectSidebarWorkflowActive,
  selectSidebarAutomationRunningKeys,
} from '../store/chatSlice'
import { isAutomationRunning } from '../monitoring/automation'
import { hasLiveSessionWork } from '../pages/chat/sessionLane'
import { normalizeRunSessionKey } from '../apps/workflows/runModel'

/**
 * One kind of live work a close would stop. The close prompt names each one
 * present, so the user reads why it asked and what confirming stops.
 *
 * Each of today's five `hasLiveSessionWork` terms maps to a named reason.
 * `work` is the generic line for a term added there later without a named
 * reason here: the decision to ask still goes through `hasLiveSessionWork`, so
 * this gate and the sidebar's Working lane keep reading the same facts.
 */
export type CloseBusyReason = 'approval' | 'turn' | 'subagents' | 'queue' | 'workflow' | 'goal_loop' | 'monitor' | 'work'

/** Reasons that come from a `hasLiveSessionWork` term. */
const LIVE_WORK_REASONS: ReadonlySet<CloseBusyReason> = new Set(['approval', 'turn', 'subagents', 'queue', 'workflow'])

/**
 * What live work closing `slotKey` would stop, in the order the prompt lists
 * it. Empty means nothing is running and the close needs no busy prompt.
 *
 * "Busy" means something `deleteSlot` would retire is still going: a running
 * turn, a turn parked on a tool approval, an armed goal loop (it reads
 * `running` FALSE between cycles), a dynamic workflow, queued messages, or
 * sub-agents that are running or accepted-but-queued. These are the signals
 * `hasLiveSessionWork` reads for the sidebar's Working lane, plus the approval,
 * the goal loop or monitor, and queued children.
 *
 * It is deliberately NOT "the sidebar lane is not idle". The Waiting lane also
 * holds a session whose last reply ended with an options card, one with an
 * unanswered question, and one whose turn was interrupted. Nothing is running
 * in those, so a close loses nothing the transcript does not keep, and asking
 * there turns a setting the user switched off into a prompt they cannot turn
 * off. A genuinely idle or waiting session stays an instant close: it is
 * reopenable from the sidebar's older-sessions list.
 */
export function closeBusyReasons(state: RootState, slotKey: string): CloseBusyReason[] {
  const reasons: CloseBusyReason[] = []
  const subagentsQueued = state.chat.subagentQueued?.[slotKey] || 0
  const slot = state.dashboard.slots.find(s => s.key === slotKey)
  // Queued children are not in `hasLiveSessionWork` (nothing has started), but
  // closing retires them too, so they count even for an unlisted slot.
  if (!slot) return subagentsQueued > 0 ? ['subagents'] : []

  const subagentsRunning = selectSidebarStartedSubagentCounts(state)[slotKey] || 0
  const subagentAwaiting = Math.min(selectSidebarApprovalCounts(state)[slotKey] || 0, subagentsRunning)
  const extras = {
    workflowActive: normalizeRunSessionKey(slotKey) in selectSidebarWorkflowActive(state),
    detailedSubagentsRunning: subagentsRunning > 0,
  }
  // A turn blocked on a tool approval, or a sub-agent blocked on one the parent
  // owes: the turn is still in flight and closing cancels it. A pending approval
  // leaves `running` true, so it replaces the "working on a reply" line rather
  // than repeating it.
  const approval = !!slot.pending_approval || subagentAwaiting > 0
  if (approval) reasons.push('approval')
  else if (slot.running) reasons.push('turn')
  if (slot.subagents_running || extras.detailedSubagentsRunning || subagentsQueued > 0) reasons.push('subagents')
  if ((slot.queue_depth ?? 0) > 0) reasons.push('queue')
  if (extras.workflowActive) reasons.push('workflow')
  // An armed goal loop or monitor is future work, not execution, so it is not
  // part of `hasLiveSessionWork`; closing still retires it. The record's kind
  // picks the line, matched on `record.slotKey`, the same field the
  // running-keys selector reads.
  if (selectSidebarAutomationRunningKeys(state).includes(slotKey)) {
    const kind = Object.values(state.chat.automations ?? {})
      .find(r => r.slotKey === slotKey && isAutomationRunning(r))?.kind
    reasons.push(kind === 'structured_monitor' ? 'monitor' : 'goal_loop')
  }
  if (hasLiveSessionWork(slot, extras) && !reasons.some(r => LIVE_WORK_REASONS.has(r))) reasons.push('work')
  return reasons
}
