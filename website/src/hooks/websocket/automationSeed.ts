/** The authoritative automation collection (legacy goal loops and structured
 *  monitors): the cold seed from the REST lists and the live `autonudge_state`
 *  frames, reconciled per slot so neither can resurrect what the other
 *  retired. */
import { useMemo, useRef } from 'react'
import type { QueryClient } from '@tanstack/react-query'
import type { AppDispatch } from '../../store'
import { setAutomations, sseAutomation, removeAutomation } from '../../store/chatSlice'
import { api } from '../../api/client'
import { AUTONUDGE_LOOPS_QUERY_KEY } from '../../components/autoNudgeLoop'
import {
  dashboardAutomationSlotKey,
  isFullLegacyAutomationRecord,
  normalizeAutomationRecord,
} from '../../monitoring/automation'
import type { FrameData } from './frames'

const LEGACY_AUTOMATION_SEED_QUERY_KEY = ['automation-seed', 'legacy'] as const
const STRUCTURED_AUTOMATION_SEED_QUERY_KEY = ['automation-seed', 'structured'] as const

export interface AutomationSeed {
  /** Cold-seed the authoritative automation collection. `autonudge_state` only
   *  fires on change, so a record armed before this client connected would be
   *  absent until its next transition. Runs on first connect and reconnect. */
  seedAutomations(): void
  onAutonudgeState(data: FrameData): void
}

export function useAutomationSeed(dispatch: AppDispatch, queryClient: QueryClient): AutomationSeed {
  /** Reconnects share one in-flight snapshot and its original watermark;
   * live frames supersede a snapshot only for their own slot. Per-slot
   * generations also retain removed-frame tombstones, so stale REST data cannot
   * resurrect a record without dropping unaffected snapshot rows. */
  const automationSeedGenRef = useRef(0)
  const automationLiveGenRef = useRef(new Map<string, number>())
  const automationSeedInFlightRef = useRef<Promise<void> | null>(null)
  const automationSeedQueuedRef = useRef(false)

  return useMemo<AutomationSeed>(() => ({
    seedAutomations() {
      // React Query coalesces equal in-flight fetches. Reuse the matching
      // orchestration too, or a reconnect would capture a newer watermark for an
      // older response and could resurrect a live tombstone. A reconnect still
      // queues one fresh snapshot because the shared request may predate changes
      // made while the socket was disconnected.
      if (automationSeedInFlightRef.current) {
        automationSeedQueuedRef.current = true
        return
      }
      const startSeed = () => {
        automationSeedQueuedRef.current = false
        const seedGen = ++automationSeedGenRef.current
        const liveAtStart = new Map(automationLiveGenRef.current)
        const cacheUpdatesAtStart = new Map<string, number>()
        for (const [queryKey] of queryClient.getQueriesData<
          ReturnType<typeof normalizeAutomationRecord>
        >({ queryKey: ['session-automation'] })) {
          if (queryKey.length === 2 && typeof queryKey[1] === 'string') {
            const slotKey = dashboardAutomationSlotKey(queryKey[1])
            cacheUpdatesAtStart.set(
              slotKey,
              queryClient.getQueryState(queryKey)?.dataUpdateCount ?? 0,
            )
          }
        }
        // Fully best-effort, including SYNCHRONOUS failure. This runs early in the
        // connect handler, ahead of notification sync and the subagent subscribe, so
        // an exception escaping here would silently strand those — a cosmetic seed
        // must never be able to do that.
        let legacy: Promise<{ loops?: unknown[] }>
        let structured: Promise<{ monitors?: unknown[] }>
        let legacyStarted = true
        let structuredStarted = true
        try {
          legacy = queryClient.fetchQuery({
            queryKey: LEGACY_AUTOMATION_SEED_QUERY_KEY,
            queryFn: api.autonudgeList,
            staleTime: 0,
            retry: false,
          })
        } catch {
          legacyStarted = false
          legacy = Promise.resolve({ loops: [] })
        }
        try {
          structured = queryClient.fetchQuery({
            queryKey: STRUCTURED_AUTOMATION_SEED_QUERY_KEY,
            queryFn: api.monitorsList,
            staleTime: 0,
            retry: false,
          })
        } catch {
          structuredStarted = false
          structured = Promise.resolve({ monitors: [] })
        }
        let seed: Promise<void>
        seed = Promise.allSettled([legacy, structured])
          .then(([legacyResult, monitorResult]) => {
            // Do not publish a snapshot known to predate a reconnect. The queued
            // iteration starts from a new watermark after this request settles.
            if (automationSeedQueuedRef.current) return
            // A later reconnect supersedes this whole seed. Live frames are
            // reconciled per slot below so unrelated snapshot rows still land.
            if (automationSeedGenRef.current !== seedGen) return
            const legacyComplete = legacyStarted && legacyResult.status === 'fulfilled'
            const structuredComplete = structuredStarted && monitorResult.status === 'fulfilled'
            const legacyRecords = legacyStarted && legacyResult.status === 'fulfilled'
              ? (legacyResult.value.loops ?? []).filter(isFullLegacyAutomationRecord)
                  .map(normalizeAutomationRecord)
                  .filter(record => record?.kind === 'legacy_goal_loop')
              : []
            const structuredSnapshotRecords = structuredStarted && monitorResult.status === 'fulfilled'
              ? (monitorResult.value.monitors ?? []).map(normalizeAutomationRecord)
                  .filter(record => record?.kind === 'structured_monitor')
              : []
            const stillFresh = (record: NonNullable<ReturnType<typeof normalizeAutomationRecord>>) =>
              (automationLiveGenRef.current.get(record.slotKey) ?? 0)
                === (liveAtStart.get(record.slotKey) ?? 0)
            const protectedSlotSet = new Set([...automationLiveGenRef.current]
              .filter(([slot, generation]) => generation !== (liveAtStart.get(slot) ?? 0))
              .map(([slot]) => slot))
            for (const [queryKey] of queryClient.getQueriesData<
              ReturnType<typeof normalizeAutomationRecord>
            >({ queryKey: ['session-automation'] })) {
              if (queryKey.length !== 2 || typeof queryKey[1] !== 'string') continue
              const slotKey = dashboardAutomationSlotKey(queryKey[1])
              const cachedUpdates = queryClient.getQueryState(queryKey)?.dataUpdateCount
              if (cachedUpdates !== cacheUpdatesAtStart.get(slotKey)) {
                protectedSlotSet.add(slotKey)
              }
            }
            const protectedSlots = [...protectedSlotSet]
            const records = [...legacyRecords, ...structuredSnapshotRecords.filter(record => record.active)]
              .filter(record => stillFresh(record) && !protectedSlotSet.has(record.slotKey))
            const terminalRecords = new Map(structuredSnapshotRecords
              .filter(record => !record.active && stillFresh(record)
                && !protectedSlotSet.has(record.slotKey))
              .map(record => [record.slotKey, record]))
            // Terminal monitors refresh existing per-slot evidence without growing
            // the global Redux collection or creating unvisited detail queries.
            const presentSlots = new Set([
              ...records.map(record => record.slotKey),
              ...structuredSnapshotRecords
                .filter(record => stillFresh(record) && !protectedSlotSet.has(record.slotKey))
                .map(record => record.slotKey),
            ])
            for (const [queryKey, cached] of queryClient.getQueriesData<
              ReturnType<typeof normalizeAutomationRecord>
            >({ queryKey: ['session-automation'] })) {
              if (queryKey.length !== 2 || typeof queryKey[1] !== 'string') continue
              const slotKey = dashboardAutomationSlotKey(queryKey[1])
              if (protectedSlotSet.has(slotKey)) continue
              // A mutation or focused REST refetch may populate this per-slot
              // cache while the reconnect snapshot is still in flight. Only an
              // entry known to predate the seed can be absent authoritatively.
              const cachedUpdates = queryClient.getQueryState(queryKey)?.dataUpdateCount
              if (cachedUpdates !== cacheUpdatesAtStart.get(slotKey)) continue
              const terminal = terminalRecords.get(slotKey)
              if (terminal) {
                queryClient.setQueryData(queryKey, terminal)
              }
              if (!cached || presentSlots.has(slotKey)) continue
              const complete = cached.kind === 'legacy_goal_loop'
                ? legacyComplete
                : structuredComplete
              if (complete) queryClient.setQueryData(queryKey, null)
            }
            dispatch(setAutomations({
              records,
              legacyComplete,
              structuredComplete,
              protectedSlots,
            }))
          })
          .catch(() => {})
          .finally(() => {
            if (automationSeedInFlightRef.current === seed) {
              automationSeedInFlightRef.current = null
              if (automationSeedQueuedRef.current) startSeed()
            }
          })
        automationSeedInFlightRef.current = seed
      }
      startSeed()
    },
    onAutonudgeState(data) {
      // One transport path feeds the authoritative collection consumed by
      // both the sidebar and active-slot detail surface.
      const nudge = data as unknown as {
        event?: string
        slot?: string
        loop?: Record<string, unknown>
      }
      if (nudge.slot) {
        const slot = dashboardAutomationSlotKey(nudge.slot)
        if (nudge.event === 'removed') {
          // A failed refetch must not leave the detail query able to
          // resurrect a record the live stream authoritatively removed.
          queryClient.setQueryData(['session-automation', slot], null)
          // The removal is authoritative for the current frame, but a
          // refresh still catches a successor created concurrently.
          queryClient.invalidateQueries({ queryKey: ['session-automation', slot] })
        }
        // Bump BEFORE dispatching so an in-flight seed is invalidated even
        // if its .then() runs immediately after this frame is handled.
        automationLiveGenRef.current.set(
          slot,
          (automationLiveGenRef.current.get(slot) ?? 0) + 1,
        )
        if (nudge.event === 'removed') {
          dispatch(removeAutomation(slot))
          queryClient.invalidateQueries({ queryKey: AUTONUDGE_LOOPS_QUERY_KEY })
          return
        }
        const record = normalizeAutomationRecord(nudge)
        if (record) {
          // The live projection is already authoritative and normalized;
          // cache it directly instead of issuing one REST request for
          // every probe frame.
          queryClient.setQueryData(['session-automation', slot], record)
          dispatch(sseAutomation(record))
        }
      }
      // Readers of the FULL registry (the Crew Members drawer's patrol
      // block needs banner, which does not ride a plain loop's frame)
      // re-read it in place rather than merging a partial payload — one
      // seed path, not a third copy of the merge.
      queryClient.invalidateQueries({ queryKey: AUTONUDGE_LOOPS_QUERY_KEY })
    },
  }), [dispatch, queryClient])
}
