/** Shared cron formatting utilities used by SchedulePage and JobForm */
import { Save, Plus } from 'lucide-react'
import { fmtDateTime, fmtWeekday } from '../i18n/format'
import type { CronJob } from '../types'
import { i18nT } from '../i18n/t'

export const TH_CLS = 'text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium'
export const TD_CLS = 'px-2.5 py-2 border-b border-border text-sm'

/** Render table header cells from column definitions */
export function renderThCells(cols: { h: string; w: string }[]) {
  return cols.map(c => <th key={c.h} className={`${TH_CLS} ${c.w}`}>{c.h}</th>)
}

export function fmtSchedule(j: CronJob): string {
  if (j.cron_expr) return j.cron_expr
  if (j.every) {
    // Bare unit letters, deliberately NOT localized: this shape ("every 3600s",
    // "1h") is the wire format the backend emits and `parseEveryFromSchedule`
    // in WeekGrid.tsx parses back with /^every\s+(\d+)\s*([sh])/. A localized
    // unit (de "3600 Sek.", bn Bengali digits) would silently fail that parse
    // and empty the schedule grid. Rendering these as a translated duration
    // requires separating the display string from the parsed one first.
    const s = j.every
    if (s < 60) return `${s}s`
    if (s < 3600) return `${Math.floor(s / 60)}m`
    if (s < 86400) return `${Math.floor(s / 3600)}h`
    return `${Math.floor(s / 86400)}d`
  }
  if (j.at) return fmtDateTime(j.at)
  return '—'
}

const DOW_NAMES: Record<string, number> = { SUN: 0, MON: 1, TUE: 2, WED: 3, THU: 4, FRI: 5, SAT: 6 }

/** Resolve a single token (numeric or named) to a cron DOW number, or -1 if invalid */
function parseDowToken(t: string): number {
  if (t === '') return -1
  const named = DOW_NAMES[t.toUpperCase()]
  if (named !== undefined) return named
  if (isNaN(+t)) return -1
  return +t  // preserve raw value (0-7); caller normalizes with %7
}

/** Expand a cron dow field (e.g. "1-5", "MON-FRI", "0,6", "MON,WED,FRI") into an array of individual numbers */
export function expandDow(dow: string): number[] {
  return [...new Set(dow.split(',').flatMap(part => {
    const m = part.match(/^([A-Za-z0-9]+)-([A-Za-z0-9]+)$/)
    if (!m) { const v = parseDowToken(part); return v < 0 ? [] : [v % 7] }
    const start = parseDowToken(m[1]), end = parseDowToken(m[2])
    if (start < 0 || end < 0) return []
    const nums: number[] = []
    if (start > end) {
      for (let i = start; i <= 6; i++) nums.push(i % 7)
      for (let i = 0; i <= end; i++) nums.push(i % 7)
    } else {
      for (let i = start; i <= end; i++) nums.push(i % 7)
    }
    return nums
  }))]
}

export function fmtCron(expr: string): string {
  try {
    const p = expr.trim().split(/\s+/)
    if (p.length !== 5) return expr
    const [min, hr, dom, , dow] = p
    // Cron day numbers are Sunday-first (0=Sun, and 7 is also Sunday), so the
    // index is converted to ISO (1=Mon…7=Sun) before asking for a name. The
    // number stays the contract; only the name is localized. English output is
    // unchanged. Anything outside 0-7 is not a weekday and falls back to the raw
    // value, exactly as the hardcoded array's sparse lookup did.
    const cronDowName = (d: number) => (d >= 0 && d <= 7 ? fmtWeekday(d === 0 ? 7 : d) : String(d))
    const expanded = expandDow(dow)
    const days = dow === '*' ? 'daily' : expanded.length > 0 ? expanded.map(cronDowName).join(',') : dow
    const domPart = dom !== '*' ? ` (days ${dom})` : ''
    return `${days} ${hr.padStart(2,'0')}:${min.padStart(2,'0')}${domPart}`
  } catch { return expr }
}

/** Save/Create button label with icon — shared by JobForm and SchedulePage */
export function SaveCreateLabel({ isEdit, saving }: { isEdit: boolean; saving: boolean }) {
  return (
    <span className="flex items-center gap-1.5">
      {isEdit ? <Save size={14} /> : <Plus size={14} />}
      {saving ? i18nT('utils.cronUtils.saving') : (isEdit ? i18nT('utils.cronUtils.save') : i18nT('utils.cronUtils.create'))}
    </span>
  )
}
