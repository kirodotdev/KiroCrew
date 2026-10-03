import { useState } from 'react'
import { useQuery, type QueryClient } from '@tanstack/react-query'

import { api } from '../../../api/client'
import {
  isFullLegacyAutomationRecord,
  normalizeAutomationRecord,
  type AutomationRecord,
} from '../../../monitoring/automation'
import { useAppSelector, type AppDispatch } from '../../../store'
import { selectAutomationForSlot, sseAutomation } from '../../../store/chatSlice'

/** Keep live liveness/counters while filling only protected scheduled text that
 * a reduced projection cannot carry. Identity equality is the merge boundary:
 * an older sibling record must never enrich or resurrect a newer automation. */
export function reconcileAutomationSources(
  live: AutomationRecord | null,
  snapshot: AutomationRecord | null | undefined,
): AutomationRecord | null {
  const full = snapshot ?? null
  if (!live) return full
  if (
    live.kind === 'legacy_goal_loop'
    && live.scheduledMessage === true
    && live.message === ''
    && full?.kind === 'legacy_goal_loop'
    && full.scheduledMessage === true
    && full.id === live.id
    && full.slotKey === live.slotKey
    && full.message !== ''
  ) {
    return { ...live, message: full.message }
  }
  return live
}

/**
 * The active session's automation (a structured monitor or a legacy goal loop):
 * the live Redux record, the cold REST snapshot that stands in for it, whether
 * the composer may offer to create one, the automation popover's open state,
 * and how the popover's edits land back in both.
 */
export function useSessionAutomation({ activeSlot, queryClient, dispatch }: {
  activeSlot: string | null
  queryClient: QueryClient
  dispatch: AppDispatch
}) {
  const [automationOpen, setAutomationOpen] = useState(false)
  const liveAutomation = useAppSelector(state => activeSlot
    ? selectAutomationForSlot(state, activeSlot)
    : null)
  const automationSnapshot = useQuery({
    queryKey: ['session-automation', activeSlot],
    enabled: !!activeSlot,
    queryFn: async () => {
      const slot = activeSlot!
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
    },
    staleTime: 0,
  })
  // Redux is the live/list projection; this query is the authoritative cold
  // read for the active slot, including retained terminal evidence that the
  // global collection intentionally does not grow to hold. Writers must
  // invalidate this query before clearing Redux. That ordering keeps creation
  // disabled while absence is being re-proved and prevents a stale snapshot
  // from replacing or resurrecting a live record.
  const automation = reconcileAutomationSources(liveAutomation, automationSnapshot.data)
  const automationId = automation?.id
  const automationCreationReady = !!automation
    || (automationSnapshot.isSuccess && !automationSnapshot.isFetching)
  const automationSnapshotFailed = automationSnapshot.isError
  /** The automation popover's change (ChatInput `onAutomationChange`). */
  const applyAutomationChange = (next: AutomationRecord | null) => {
    if (next) {
      queryClient.setQueryData(['session-automation', next.slotKey], next)
      dispatch(sseAutomation(next))
    }
    else if (automation?.kind === 'legacy_goal_loop') {
      queryClient.setQueryData(['session-automation', automation.slotKey], null)
      dispatch(sseAutomation({ ...automation, active: false }))
    }
  }
  return {
    automationOpen, setAutomationOpen, automation, automationId, automationCreationReady, automationSnapshotFailed,
    applyAutomationChange,
  }
}
