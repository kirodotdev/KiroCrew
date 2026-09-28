/** Monitor and goal-loop automations per slot, reconciled from the REST
 *  snapshots and upserted from live frames through one normalizer. */
import { createSelector, type PayloadAction } from '@reduxjs/toolkit'
import type { RootState } from '../index'
import { automationForSlot, type AutomationRecord } from '../../monitoring/automation'
import type { ChatState } from './state'
import { isUnsafeKey, safeKey } from './wire'

/** Slot keys with a live automation. Memoization keeps the sidebar shell from
 * repainting when only a probe count or terminal detail changes. */
export const selectSidebarAutomationRunningKeys = createSelector(
  [(state: RootState) => state.chat.automations],
  (automations) => Object.values(automations ?? {})
    .filter(record => record.kind === 'legacy_goal_loop'
      ? record.active
      : record.active && !record.terminal)
    .map(record => record.slotKey),
)

export function selectAutomationForSlot(
  state: { chat: Pick<ChatState, 'automations'> },
  slotKey: string,
): AutomationRecord | null {
  if (isUnsafeKey(slotKey)) return null
  return automationForSlot(state.chat.automations, safeKey(slotKey))
}

export const automationReducers = {
  /** Reconcile whichever independent REST snapshots completed successfully.
   * A failed read is unknown, not an authoritative empty collection. */
  setAutomations(state: ChatState, action: PayloadAction<{
    records: AutomationRecord[]
    legacyComplete: boolean
    structuredComplete: boolean
    protectedSlots?: string[]
  }>) {
    const next: Record<string, AutomationRecord> = { ...(state.automations ?? {}) }
    const protectedSlots = new Set(action.payload.protectedSlots ?? [])
    for (const [key, record] of Object.entries(next)) {
      if (!protectedSlots.has(record.slotKey)
        && ((record.kind === 'legacy_goal_loop' && action.payload.legacyComplete)
        || (record.kind === 'structured_monitor' && action.payload.structuredComplete))) {
        delete next[key]
      }
    }
    for (const record of action.payload.records) {
      if (isUnsafeKey(record.slotKey)) continue
      if (record.kind === 'legacy_goal_loop' && !record.active) continue
      next[safeKey(record.slotKey)] = record
    }
    state.automations = next
  },
  /** Upsert one normalized WS or mutation result into the same collection. */
  sseAutomation(state: ChatState, action: PayloadAction<AutomationRecord>) {
    const record = action.payload
    if (isUnsafeKey(record.slotKey)) return
    state.automations ??= {}
    if (record.kind === 'legacy_goal_loop' && !record.active) {
      delete state.automations[safeKey(record.slotKey)]
      return
    }
    state.automations[safeKey(record.slotKey)] = record
  },
  removeAutomation(state: ChatState, action: PayloadAction<string>) {
    if (isUnsafeKey(action.payload)) return
    state.automations ??= {}
    delete state.automations[safeKey(action.payload)]
  },
}
