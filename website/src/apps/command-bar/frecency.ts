/**
 * Frecency for Command Bar root rows.
 *
 * The root list is ranked by how often and how recently a row was used, not by
 * name. That ranking is what removes the need for prefix sigils: a command the
 * user runs daily surfaces within one or two keystrokes, so there is nothing left
 * for a `$`/`@` prefix to save. Recency decays on a half-life rather than a
 * cutoff, so a command used heavily last month loses to today's without ever
 * disappearing.
 *
 * Usage counts are a per-browser preference, so they live in localStorage and
 * every access is guarded: a storage quota error or a privacy mode that throws on
 * read must degrade to "no usage history", never break the launcher.
 */

import { resolveLegacyHighlightId } from '../../hooks/useSettingHighlight'

const STORAGE_KEY = 'mc-command-bar-frecency'

/** Days after which a single use counts half as much. */
const HALF_LIFE_MS = 14 * 24 * 60 * 60 * 1000

/** Rows kept in the store. Beyond this the least valuable entries are dropped so
 * the key cannot grow without bound in a long-lived browser profile. */
const MAX_ENTRIES = 300

export interface UsageEntry {
  /** Number of recorded activations. */
  count: number
  /** Epoch ms of the most recent activation. */
  last: number
}

export type UsageMap = Record<string, UsageEntry>

function isUsageEntry(v: unknown): v is UsageEntry {
  if (typeof v !== 'object' || v === null) return false
  const e = v as Partial<UsageEntry>
  return typeof e.count === 'number' && typeof e.last === 'number'
}

/** The row id of a settings registry entry is this prefix plus its id. */
export const SETTING_ROW_PREFIX = 'setting:'

/**
 * A settings row's id follows its label, so a relabelled row reads (and the
 * next save writes) its old history under the id it has now, through the same
 * legacy-id table its deep links use.
 */
function currentRowId(id: string): string {
  return id.startsWith(SETTING_ROW_PREFIX)
    ? SETTING_ROW_PREFIX + resolveLegacyHighlightId(id.slice(SETTING_ROW_PREFIX.length))
    : id
}

/** Read the usage map. Returns an empty map on absent or unreadable storage. */
export function loadUsage(): UsageMap {
  let raw: string | null = null
  try {
    raw = localStorage.getItem(STORAGE_KEY)
  } catch {
    return {}
  }
  if (!raw) return {}
  try {
    const parsed: unknown = JSON.parse(raw)
    if (typeof parsed !== 'object' || parsed === null) return {}
    const out: UsageMap = {}
    for (const [id, entry] of Object.entries(parsed as Record<string, unknown>)) {
      if (isUsageEntry(entry)) out[id] = { count: entry.count, last: entry.last }
    }
    return renameUsageIds(out, currentRowId)
  } catch {
    return {}
  }
}

function saveUsage(map: UsageMap): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(map))
  } catch {
    // A full or read-only store costs ranking quality, nothing else.
  }
}

/**
 * The same map with each id passed through `rename`, merging two entries that
 * land on one id (counts add, the later use wins). A renamed row keeps the
 * habit it built up under its old id.
 */
function renameUsageIds(map: UsageMap, rename: (id: string) => string): UsageMap {
  const out: UsageMap = {}
  for (const [id, entry] of Object.entries(map)) {
    const to = rename(id)
    const prev = out[to]
    out[to] = prev ? { count: prev.count + entry.count, last: Math.max(prev.last, entry.last) } : entry
  }
  return out
}

/**
 * Score one row. Higher wins. An unused row scores 0, so it ranks purely on
 * match quality — a first-time command is never buried by the ordering, it just
 * does not get a boost.
 */
export function frecencyScore(entry: UsageEntry | undefined, now = Date.now()): number {
  if (!entry || entry.count <= 0) return 0
  const age = Math.max(0, now - entry.last)
  return entry.count * Math.pow(2, -age / HALF_LIFE_MS)
}

/**
 * Record an activation and persist. Returns the updated map so a caller holding
 * it in state does not have to re-read storage.
 */
export function recordUse(id: string, now = Date.now(), current?: UsageMap): UsageMap {
  const map = { ...(current ?? loadUsage()) }
  const prev = map[id]
  map[id] = { count: (prev?.count ?? 0) + 1, last: now }
  const ids = Object.keys(map)
  if (ids.length > MAX_ENTRIES) {
    // Drop the lowest-scoring entries rather than the oldest: a rarely-used row
    // touched yesterday is less worth keeping than a daily one touched last week.
    const ranked = ids.sort((a, b) => frecencyScore(map[b], now) - frecencyScore(map[a], now))
    for (const stale of ranked.slice(MAX_ENTRIES)) delete map[stale]
  }
  saveUsage(map)
  return map
}

/** Test seam: forget all recorded usage. */
export function _clearUsageForTest(): void {
  try {
    localStorage.removeItem(STORAGE_KEY)
  } catch {
    // nothing to clear
  }
}
