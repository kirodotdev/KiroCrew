import type { RootState } from '../store'
import {
  selectSidebarStartedSubagentCounts,
  selectSidebarApprovalCounts,
  selectSidebarWorkflowActive,
  selectSidebarAutomationRunningKeys,
} from '../store/chatSlice'
import { inferLane } from '../pages/chat/sessionLane'
import { normalizeRunSessionKey } from '../apps/workflows/runModel'

/**
 * Whether closing `slotKey` would stop live work, so the close must ask first
 * whatever the user's `confirmCloseSession` setting says.
 *
 * "Busy" is the sidebar's own lane inference, not `slot.running`: that flag
 * covers only the slot's own turn and reads FALSE between the cycles of an
 * armed goal loop, during a dynamic workflow, and while background sub-agents
 * run — all of which `deleteSlot` retires. Reusing `inferLane` with the same
 * extras the sidebar computes keeps this gate and the Working / Waiting /
 * Needs-approval lanes from ever disagreeing. Queued children are not in the
 * lane (nothing has started), but closing retires them too, so they count on
 * their own term. An idle session stays an instant close: it is reopenable
 * from the sidebar's older-sessions list with nothing lost.
 */
export function sessionIsBusyForClose(state: RootState, slotKey: string): boolean {
  const slot = state.dashboard.slots.find(s => s.key === slotKey)
  const subagentsRunning = selectSidebarStartedSubagentCounts(state)[slotKey] || 0
  const subagentsQueued = state.chat.subagentQueued?.[slotKey] || 0
  const lane = slot ? inferLane(slot, {
    subagentAwaiting: Math.min(selectSidebarApprovalCounts(state)[slotKey] || 0, subagentsRunning),
    workflowActive: normalizeRunSessionKey(slotKey) in selectSidebarWorkflowActive(state),
    goalLoopActive: selectSidebarAutomationRunningKeys(state).includes(slotKey),
    detailedSubagentsRunning: subagentsRunning > 0,
  }) : 'idle'
  return lane !== 'idle' || subagentsQueued > 0
}
