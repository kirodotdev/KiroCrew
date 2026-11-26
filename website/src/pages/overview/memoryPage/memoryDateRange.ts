/**
 * A date range on the memory page: a quick range or a from–to pair of local days,
 * turned into the UTC bounds the server filters on (`since` inclusive, `until`
 * exclusive). A quick range is kept as its name, so "Last 7 days" still means the
 * last seven days tomorrow.
 */

/** `YYYY-MM-DD` of a local date. */
export function dayKey(d: Date): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

/** The local date a `YYYY-MM-DD` key names, or null for anything else. */
export function dayDate(day: string): Date | null {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(day)
  if (!m) return null
  const date = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]))
  return date.getMonth() === Number(m[2]) - 1 ? date : null
}

export type RangePreset = 'today' | '7d' | '30d' | 'custom'
export type DateRange = { preset: RangePreset; from?: string; to?: string }
export const RANGE_PRESETS: Exclude<RangePreset, 'custom'>[] = ['today', '7d', '30d']
const DAYS_BACK: Record<Exclude<RangePreset, 'custom'>, number> = { today: 0, '7d': 6, '30d': 29 }

/** The server bounds for a range, or null when it names no valid days. */
export function rangeBounds(range: DateRange | undefined, now: Date = new Date()): { since?: string; until?: string } | null {
  if (!range) return null
  if (range.preset !== 'custom') {
    const start = new Date(now.getFullYear(), now.getMonth(), now.getDate() - DAYS_BACK[range.preset])
    return { since: start.toISOString() }
  }
  const from = range.from ? dayDate(range.from) : null
  const to = range.to ? dayDate(range.to) : null
  if (!from && !to) return null
  if (from && to && from > to) return null
  return {
    ...(from ? { since: from.toISOString() } : {}),
    ...(to ? { until: new Date(to.getFullYear(), to.getMonth(), to.getDate() + 1).toISOString() } : {}),
  }
}

/** A saved range read back, or undefined for anything that is not one. */
export function parseRange(raw: unknown): DateRange | undefined {
  if (!raw || typeof raw !== 'object') return undefined
  const { preset, from, to } = raw as Record<string, unknown>
  if (preset === 'today' || preset === '7d' || preset === '30d') return { preset }
  if (preset !== 'custom') return undefined
  const range: DateRange = { preset, ...(typeof from === 'string' && dayDate(from) ? { from } : {}), ...(typeof to === 'string' && dayDate(to) ? { to } : {}) }
  return rangeBounds(range) ? range : undefined
}
