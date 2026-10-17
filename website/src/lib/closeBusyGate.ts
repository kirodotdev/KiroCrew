import type { RootState } from '../store'
import {
  selectSidebarStartedSubagentCounts,
  selectSidebarApprovalCounts,
  selectSidebarWorkflowActive,
  selectSidebarAutomationRunningKeys,
} from '../store/chatSlice'
import { hasLiveSessionWork } from '../pages/chat/sessionLane'
import { normalizeRunSessionKey } from '../apps/workflows/runModel'

/**
 * Whether closing `slotKey` would stop live work, so the close must ask first
 * whatever the user's `confirmCloseSession` setting says.
 *
 * "Busy" means something `deleteSlot` would retire is still going: a running
 * turn, a turn parked on a tool approval, an armed goal loop (it reads
 * `running` FALSE between cycles), a dynamic workflow, or sub-agents that are
 * running or accepted-but-queued. `hasLiveSessionWork` supplies the shared
 * execution signals so this gate and the sidebar's Working lane read the same
 * facts.
 *
 * It is deliberately NOT "the sidebar lane is not idle". The Waiting lane also
 * holds a session whose last reply ended with an options card, one with an
 * unanswered question, and one whose turn was interrupted. Nothing is running
 * in those, so a close loses nothing the transcript does not keep, and asking
 * there turns a setting the user switched off into a prompt they cannot turn
 * off. A genuinely idle or waiting session stays an instant close: it is
 * reopenable from the sidebar's older-sessions list.
 */
export function sessionIsBusyForClose(state: RootState, slotKey: string): boolean {
  const subagentsQueued = state.chat.subagentQueued?.[slotKey] || 0
  // Queued children are not in `hasLiveSessionWork` (nothing has started), but
  // closing retires them too, so they count on their own term.
  if (subagentsQueued > 0) return true

  const slot = state.dashboard.slots.find(s => s.key === slotKey)
  if (!slot) return false

  const subagentsRunning = selectSidebarStartedSubagentCounts(state)[slotKey] || 0
  const subagentAwaiting = Math.min(selectSidebarApprovalCounts(state)[slotKey] || 0, subagentsRunning)
  // A turn blocked on a tool approval, or a sub-agent blocked on one the parent
  // owes: the turn is still in flight and closing cancels it.
  if (slot.pending_approval || subagentAwaiting > 0) return true
  // An armed goal loop is future work, not execution, so it is not part of
  // `hasLiveSessionWork`; closing still retires it.
  if (selectSidebarAutomationRunningKeys(state).includes(slotKey)) return true

  return hasLiveSessionWork(slot, {
    workflowActive: normalizeRunSessionKey(slotKey) in selectSidebarWorkflowActive(state),
    detailedSubagentsRunning: subagentsRunning > 0,
  })
}
