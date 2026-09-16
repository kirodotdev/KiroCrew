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
  todaySessions,
} from './todayActivity'

/** Closed rows are newest-first, so one page this size holds any day's worth
 *  of sessions with room to spare; the today filter drops the rest. */
const CLOSED_ROWS_LIMIT = 100

interface ClosedSessionsResponse {
  sessions?: ClosedSessionRow[]
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
 * already holds: live slots from redux, the closed-session list and folders
 * the sidebar also reads (same query keys, so the caches are shared), the
 * day's history file, and the consolidation idle setting. No model call.
 */
export function useTodayActivity(): TodayActivity {
  const slots = useAppSelector(s => s.dashboard.slots)
  // Re-derived per render: the local day can roll over while the page is open,
  // and a changed key both refetches the history and recomputes the memo.
  const dateKey = localDateKey(new Date())

  const closedQ = useQuery<ClosedSessionsResponse | ClosedSessionRow[]>({
    queryKey: ['sessions-closed', CLOSED_ROWS_LIMIT],
    queryFn: () => api.sessions(CLOSED_ROWS_LIMIT, 0, false, true),
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
    const closed = Array.isArray(closedRows) ? closedRows : closedRows?.sessions ?? []
    const sessions = todaySessions(slots, closed, new Date())
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
