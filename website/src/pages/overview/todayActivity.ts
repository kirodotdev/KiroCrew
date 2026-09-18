import type { ChatFolder, ChatSlot } from '../../types'
import { lastActivityEpoch, localDaysAgo } from '../chat/sessionOrder'

/**
 * Data shaping for the Overview "Today" card and its `?view=today` drill-in.
 *
 * Pure functions over data the dashboard already holds: the live slots the
 * sidebar renders from redux, the closed-session rows `/api/sessions` returns,
 * the folder list, and the day file the memory pipeline writes. No model call
 * anywhere on this path -- the card is a view over existing state.
 */

/** The subset of a closed-session row (`/api/sessions?exclude_open=1`) read here. */
export interface ClosedSessionRow {
  key: string
  title?: string
  created?: string
  /** Backend mtime, epoch seconds. */
  modified?: number
  folder_id?: string
  memory_mode?: 'persistent' | 'incognito' | 'temporary'
}

/** One session row of the Today list. `messages` is present only where the
 *  count is exact (a live slot); a closed row's server-side count is a size
 *  estimate, so it is not shown. */
export interface TodaySession {
  key: string
  title: string
  folder_id?: string
  /** Last activity, epoch seconds. */
  activity: number
  live: boolean
  running: boolean
  messages?: number
}

export interface TodayGroup {
  /** `''` for the unfiled group, which always sorts last. */
  folder_id: string
  name?: string
  sessions: TodaySession[]
}

/** `YYYY-MM-DD` from LOCAL date parts, not `toISOString` (which is UTC): east
 *  of UTC near midnight the UTC date is still yesterday, and the card would ask
 *  for a day the user does not see. */
export function localDateKey(now: Date): string {
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`
}

function isToday(epochSeconds: number, now: Date): boolean {
  if (!epochSeconds) return false
  // A future instant (clock skew between gateway and browser) folds into today.
  return localDaysAgo(now, new Date(epochSeconds * 1000)) <= 0
}

/** True when a newest-first page ends with a row that predates today: every
 *  row on a later page is older still, so the caller can stop paging. An
 *  empty page has no such row and returns false. */
export function reachesBeforeToday(rows: readonly ClosedSessionRow[], now: Date): boolean {
  const last = rows[rows.length - 1]
  return last !== undefined && !isToday(lastActivityEpoch(last), now)
}

/** Incognito and temporary sessions never appear: the server already omits
 *  them from the closed-session list, and a live slot in either mode is a
 *  conversation the user asked not to keep. */
function isListable(mode: ChatSlot['memory_mode'] | undefined): boolean {
  return mode !== 'incognito' && mode !== 'temporary'
}

/**
 * Sessions with activity in the browser's local today: every live slot that
 * moved today plus the closed rows modified today. A key present in both is
 * the same session and the live slot wins (exact message count, running state).
 * Newest activity first.
 */
export function todaySessions(
  slots: readonly ChatSlot[],
  closed: readonly ClosedSessionRow[],
  now: Date,
): TodaySession[] {
  const seen = new Set<string>()
  const out: TodaySession[] = []
  const nowEpoch = now.getTime() / 1000
  for (const slot of slots) {
    if (!isListable(slot.memory_mode)) continue
    // A slot with a turn in flight is active NOW: `last_turn_ts` moves only when
    // a prompt arrives or a turn ends, so a turn that started yesterday and is
    // still streaming today would otherwise date the session to yesterday.
    const activity = slot.running ? nowEpoch : lastActivityEpoch(slot)
    seen.add(slot.key)
    if (!isToday(activity, now)) continue
    out.push({
      key: slot.key,
      title: slot.title || slot.key,
      folder_id: slot.folder_id,
      activity,
      live: true,
      running: !!slot.running,
      messages: slot.messages,
    })
  }
  for (const row of closed) {
    if (seen.has(row.key) || !isListable(row.memory_mode)) continue
    seen.add(row.key)
    const activity = lastActivityEpoch(row)
    if (!isToday(activity, now)) continue
    out.push({
      key: row.key,
      title: row.title || row.key,
      folder_id: row.folder_id,
      activity,
      live: false,
      running: false,
    })
  }
  return out.sort((a, b) => b.activity - a.activity)
}

/**
 * Group by folder, folders ordered by their most recent session, the unfiled
 * group last. A `folder_id` that names no known folder (deleted folder, stale
 * row) is treated as unfiled rather than rendering a headerless group.
 */
export function groupByFolder(sessions: readonly TodaySession[], folders: readonly ChatFolder[]): TodayGroup[] {
  const names = new Map(folders.map(f => [f.id, f.name]))
  const groups = new Map<string, TodayGroup>()
  for (const s of sessions) {
    const id = s.folder_id && names.has(s.folder_id) ? s.folder_id : ''
    let g = groups.get(id)
    if (!g) {
      g = { folder_id: id, name: id ? names.get(id) : undefined, sessions: [] }
      groups.set(id, g)
    }
    g.sessions.push(s)
  }
  // Input is newest-first, so a group's first session is its newest; Map keeps
  // insertion order, which is therefore already newest-group-first.
  const ordered = [...groups.values()]
  const unfiled = ordered.findIndex(g => g.folder_id === '')
  if (unfiled >= 0) ordered.push(...ordered.splice(unfiled, 1))
  return ordered
}

/** One `#### HH:MM TZ` block of a daily history file. */
export interface HistoryEntry {
  time: string
  text: string
}

/**
 * Split a day file (`# YYYY-MM-DD` header, then `#### HH:MM TZ` blocks, the
 * shape `append_history` writes) into entries, newest first. The header and
 * anything before the first block are dropped; a block with no body is kept
 * out rather than rendered as an empty row.
 */
export function parseHistoryEntries(content: string): HistoryEntry[] {
  const entries: HistoryEntry[] = []
  const blocks = content.split(/^#### /m)
  for (const block of blocks.slice(1)) {
    const newline = block.indexOf('\n')
    const time = (newline < 0 ? block : block.slice(0, newline)).trim()
    const text = (newline < 0 ? '' : block.slice(newline + 1)).trim()
    if (!text) continue
    entries.push({ time, text })
  }
  return entries.reverse()
}
