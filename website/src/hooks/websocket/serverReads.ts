/** Unread from the gateway's read times, so every device agrees. `read_epoch` is a floor, never a read: it clears no badge, and a row without one badges nothing. */
import type { QueryClient } from '@tanstack/react-query'
import { store, type AppDispatch } from '../../store'
import type { ChatSlot } from '../../types'
import { markSlotUnread, remoteSlotRead } from '../../store/dashboardSlice'
import { MANUAL_UNREAD, readCovers } from '../../store/dashboard/unread'
import { ackRead, queuedReads } from '../../lib/readOutbox'
import { newerTs, shownHere } from '../../lib/slotReadRelay'
import { appsMutingSessions, isSlotMuted } from '../sessionMute'
import { loadUnreadOnAttention } from '../unreadOnAttention'
import { crewWorkerRoot, isSlotOnScreen } from './attention'
import { slotLastTsOf } from '../../store/slotLastTs'

/** The gateway's newest row that should badge *row* unseen; under "only when done or waiting" an idle chat badges on its turn's end and a busy turn skips, as live. */
export function unreadCandidateTs(row: ChatSlot, attentionOn: boolean): string | undefined {
  const waiting = row.pending_approval || row.needs_input
  if (attentionOn && row.running && !waiting) return undefined
  if (!attentionOn || waiting || row.mode === 'member') return row.unread_ts || undefined
  return (row.unread_failed ? row.unread_ts : row.last_agent_ts) || undefined
}

const tsOf = (value: string | null | undefined): string | undefined =>
  typeof value === 'string' && value ? value : undefined

export function clearCovered(row: ChatSlot, keptRead: string | undefined, dispatch: AppDispatch): string | undefined {
  const serverRead = tsOf(row.last_read_ts)
  if (keptRead !== undefined && serverRead !== undefined && readCovers(serverRead, keptRead)) {
    ackRead(row.key, serverRead)
    keptRead = undefined
  }
  const read = newerTs(newerTs(serverRead, keptRead), shownHere(row.key))
  if (read === undefined) return undefined
  const dashboard = store.getState().dashboard
  if (!dashboard.unreadSlots.includes(row.key)) return read
  const since = dashboard.unreadSince?.[row.key]
  if (since === MANUAL_UNREAD) return read
  const guard = since ?? slotLastTsOf(dashboard, row.key)
  if (guard && readCovers(read, guard)) dispatch(remoteSlotRead({ slot: row.key, readTs: read }))
  return read
}

/** Badge the live conductor of a crew member waiting on an approval, as its live rule does. */
function badgeCrewConductor(row: ChatSlot, epoch: string, dispatch: AppDispatch): void {
  const root = row.pending_approval ? crewWorkerRoot(row.key) : ''
  const candidate = tsOf(row.last_agent_ts)
  if (!root || !candidate || isSlotOnScreen(root)) return
  const rootRow = store.getState().dashboard.slots.find(s => s.key === root)
  const read = newerTs(newerTs(tsOf(rootRow?.last_read_ts), queuedReads()[root]), shownHere(root))
  if (readCovers(newerTs(read, epoch), candidate)) return
  dispatch(markSlotUnread({ slot: root, ts: candidate }))
}

/** Badge *row* past *read* and its `read_epoch`. With *mutedApps* still unknown, every app counts as muting. */
export function badgeMissed(
  row: ChatSlot,
  read: string | undefined,
  { dispatch, attentionOn, mutedApps }: { dispatch: AppDispatch; attentionOn: boolean; mutedApps: ReadonlySet<string> | undefined },
): void {
  const epoch = tsOf(row.read_epoch)
  if (epoch === undefined) return
  badgeCrewConductor(row, epoch, dispatch)
  const floor = newerTs(read, epoch)
  const candidate = unreadCandidateTs(row, attentionOn)
  if (!candidate || readCovers(floor, candidate)) return
  const slots = store.getState().dashboard.slots
  const apps = mutedApps ?? new Set(slots.flatMap(s => (s.app ? [s.app] : [])))
  if (isSlotOnScreen(row.key) || (isSlotMuted(slots, row.key, apps) && !(mutedApps && row.unread_failed))) return
  dispatch(markSlotUnread({ slot: row.key, ts: candidate }))
}

export interface ServerReadDeps {
  dispatch: AppDispatch
  queryClient: QueryClient
}

/** Returns false when the app mutes were still unknown, so app badges were held back. */
export function applyServerReads(rows: readonly ChatSlot[], { dispatch, queryClient }: ServerReadDeps): boolean {
  const kept = queuedReads()
  const attentionOn = loadUnreadOnAttention()
  const config = queryClient.getQueryData(['kirocrewConfig'])
  const mutedApps = config === undefined ? undefined : appsMutingSessions(config)
  for (const row of rows) {
    const read = clearCovered(row, kept[row.key], dispatch)
    badgeMissed(row, read, { dispatch, attentionOn, mutedApps })
  }
  return mutedApps !== undefined
}
