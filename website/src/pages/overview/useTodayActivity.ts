import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '../../api/client'
import { useAppSelector } from '../../store'
import type { ChatFolder } from '../../types'
import {
  type ClosedSessionRow,
  type HistoryEntry,
  type TodayGroup,
  type TodaySession,
  groupByFolder,
  localDateKey,
  parseHistoryEntries,
  reachesBeforeToday,
  todaySessions,
} from './todayActivity'

/** Page size for the closed-session walk: the endpoint's maximum. */
const CLOSED_PAGE_SIZE = 200
/** Pages the walk reads before stopping on its own (2,000 rows). More
 *  archived sessions than that touched in one day is not a number the card
 *  can describe anyway; the bound keeps a pathological store from turning
 *  the landing page into a full-inventory scan. */
const CLOSED_MAX_PAGES = 10

interface ClosedSessionsResponse {
  sessions?: ClosedSessionRow[]
  has_more?: boolean
}

type ClosedSessionsPage = ClosedSessionsResponse | ClosedSessionRow[]

/**
 * Every closed row that can be today's. The list is newest-first, so the walk
 * reads pages until one ends with a row that predates today, the endpoint
 * reports no more rows, or the page bound is reached; `todaySessions` drops
 * the older tail of the last page. One fixed page would silently truncate the
 * count on a day with more sessions than the page holds.
 */
export async function fetchClosedRowsForToday(
  now: Date,
  list: (limit: number, offset: number, preview: boolean, excludeOpen: boolean) => Promise<ClosedSessionsPage> = api.sessions,
): Promise<ClosedSessionRow[]> {
  const rows: ClosedSessionRow[] = []
  for (let page = 0; page < CLOSED_MAX_PAGES; page++) {
    const res = await list(CLOSED_PAGE_SIZE, page * CLOSED_PAGE_SIZE, false, true)
    const batch = Array.isArray(res) ? res : res?.sessions ?? []
    rows.push(...batch)
    const hasMore = !Array.isArray(res) && res?.has_more === true
    if (!hasMore || batch.length === 0 || reachesBeforeToday(batch, now)) break
  }
  return rows
}

export interface TodayActivity {
  dateKey: string
  sessions: TodaySession[]
  groups: TodayGroup[]
  running: number
  /** Epoch seconds of the newest session activity, `0` when there is none. */
  lastActivity: number
  entries: HistoryEntry[]
  /** Consolidation idle window from the same setting the Memory tab edits. */
  idleHours: number
  /** True until the first answer from the closed-session list arrives. */
  loading: boolean
  /** The dated history read failed (the sessions half still renders). */
  historyError: Error | null
  sessionsError: Error | null
}

/**
 * Everything the Today card and its drill-in show, from data the dashboard
 * already holds: live slots from redux, the closed-session list (this hook's
 * own query, walked to the day boundary), the folder list (the `['chat-folders']`
 * cache the sidebar and the command palette share), the day's history file,
 * and the consolidation idle setting. No model call.
 */
export function useTodayActivity(): TodayActivity {
  const slots = useAppSelector(s => s.dashboard.slots)
  // Re-derived per render: the local day can roll over while the page is open,
  // and a changed key refetches the history, re-walks the closed rows and
  // recomputes the memo.
  const dateKey = localDateKey(new Date())

  const closedQ = useQuery<ClosedSessionRow[]>({
    queryKey: ['sessions-closed-today', dateKey],
    queryFn: () => fetchClosedRowsForToday(new Date()),
    staleTime: 30_000,
  })
  const foldersQ = useQuery<ChatFolder[]>({
    queryKey: ['chat-folders'],
    queryFn: () => api.chatFolders(),
  })
  const historyQ = useQuery<{ content?: string }>({
    queryKey: ['memory-history', dateKey],
    queryFn: () => api.memoryHistory(undefined, dateKey),
    staleTime: 60_000,
  })
  const settingsQ = useQuery<{ history_idle_hours?: number }>({
    queryKey: ['memory-settings'],
    queryFn: () => api.memorySettings(),
  })

  const closedRows = closedQ.data
  const folders = foldersQ.data
  const historyContent = historyQ.data?.content
  return useMemo(() => {
    const sessions = todaySessions(slots, closedRows ?? [], new Date())
    const groups = groupByFolder(sessions, Array.isArray(folders) ? folders : [])
    return {
      dateKey,
      sessions,
      groups,
      running: sessions.filter(s => s.running).length,
      lastActivity: sessions[0]?.activity ?? 0,
      entries: parseHistoryEntries(historyContent ?? ''),
      idleHours: settingsQ.data?.history_idle_hours ?? 3,
      loading: closedQ.isPending,
      historyError: historyQ.error,
      sessionsError: closedQ.error,
    }
  }, [slots, closedRows, folders, historyContent, settingsQ.data, closedQ.isPending, closedQ.error, historyQ.error, dateKey])
}
