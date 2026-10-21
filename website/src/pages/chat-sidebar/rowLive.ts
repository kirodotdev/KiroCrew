/** The live-facts selector each sidebar session row (`SessionRow`) subscribes with. */
import type { RootState } from '../../store'
import { selectSidebarWorkflowActive, selectAutomationForSlot } from '../../store/chatSlice'
import { normalizeRunSessionKey } from '../../apps/workflows/runModel'

/** A row's slot-scoped live facts, read in ONE subscription. Flat, so the
 *  caller's shallowEqual compares each fact by identity: the workflow entry
 *  (rebuilt per run event) is spread into primitives for the same reason the
 *  old per-field subscription compared it shallowly. */
export function selectRowLive(st: RootState, localSlotKey: string) {
  const wf = selectSidebarWorkflowActive(st)[normalizeRunSessionKey(localSlotKey)]
  return {
    statusDetail: st.chat.slotStatusDetail?.[localSlotKey],
    automation: selectAutomationForSlot(st, localSlotKey),
    queuedForSlot: st.chat.subagentQueued?.[localSlotKey] || 0,
    queuedReason: st.chat.subagentQueuedReason?.[localSlotKey],
    wfCount: wf?.count,
    wfName: wf?.name,
    wfPhase: wf?.phase,
  }
}
