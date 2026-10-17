import { api } from '../api/client'
import {
  isFullLegacyAutomationRecord,
  normalizeAutomationRecord,
  type AutomationRecord,
} from './automation'

/** One slot's authoritative automation record from its two REST reads.
 *  Shared by the chat page and the crewmate Profile; throws on a malformed or
 *  conflicting pair so neither surface can invent a state. */
export async function readSessionAutomationSnapshot(slot: string): Promise<AutomationRecord | null> {
  const [legacy, structured] = await Promise.all([
    api.autonudgeForSlot(slot),
    api.monitorForSlot(slot),
  ])
  const hasFullLegacyRecord = legacy.loop !== null
    && isFullLegacyAutomationRecord(legacy.loop)
  const legacySnapshot = !hasFullLegacyRecord
    ? null
    : normalizeAutomationRecord(legacy.loop)
  const structuredRecord = structured.monitor === null
    ? null
    : normalizeAutomationRecord(structured.monitor)
  if ((hasFullLegacyRecord && !legacySnapshot)
    || (structured.monitor !== null
      && structuredRecord?.kind !== 'structured_monitor')) {
    throw new Error('Invalid session automation snapshot')
  }
  const legacyRecord = legacySnapshot?.kind === 'legacy_goal_loop'
    ? legacySnapshot
    : null
  if (legacyRecord && structuredRecord) {
    throw new Error('Conflicting session automation snapshot')
  }
  return structuredRecord ?? legacyRecord
}
