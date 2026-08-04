import { useMemo, useEffect, useState } from 'react'
import type { CronJob } from '../types'
import { convertCronTime, hourFractionInTz, todayCronDowInTz } from '../utils/tz'
import { fmtDateFields, fmtWeekday } from '../i18n/format'

/** Grid columns, Monday-first. `DAY_COUNT` carries the index contract that
 *  CRON_TO_GRID maps into; `dayLabel(i)` renders the localized name. Splitting
 *  them keeps every `DAYS.map((_, i) => …)` slot computation byte-identical
 *  while the visible header becomes translatable. */
const DAY_COUNT = 7
const DAY_INDEXES = Array.from({ length: DAY_COUNT }, (_, i) => i)
const dayLabel = (gridIndex: number) => fmtWeekday(gridIndex + 1)
// Cron DOW: 0=Sun,1=Mon..6=Sat → our grid: 0=Mon..6=Sun
const CRON_TO_GRID: Record<number, number> = { 1: 0, 2: 1, 3: 2, 4: 3, 5: 4, 6: 5, 0: 6 }

interface Slot { job: CronJob; day: number; hour: number; minute: number }

/** Extract interval seconds from a human-readable schedule string like "every 3600s" or "every 1h". */
function parseEveryFromSchedule(s: string): number | null {
  const m = (s || '').match(/^every\s+(\d+)\s*([sh])/)
  if (!m) return null
  const n = parseInt(m[1])
  return m[2] === 'h' ? n * 3600 : n
}

/** Parse a cron schedule string into grid slots (day index 0-6, hour, minute).
 *
 *  Each job stores its cron `hour`/`minute` fields in the job's own
 *  IANA timezone (falling back to UTC for legacy jobs). This function
 *  converts those fields from the job's TZ to `renderTz` so the grid
 *  positions match the user's chosen render timezone. */
export function parseCronSlots(job: CronJob, renderTz: string): Slot[] {
  // Interval-based: use every_secs field, or parse from schedule string
  const everySecs = job.every_secs || parseEveryFromSchedule(job.schedule)
  if (everySecs && everySecs > 0) {
    const secs = everySecs
    // Interval jobs have no "clock-time" notion — anchor in renderTz for
    // a stable position regardless of which TZ authored them.
    if (secs >= 86400) {
      const anchorTs = job.last_run_ts || job.created_ts
      const ref = anchorTs ? new Date(anchorTs * 1000) : new Date()
      const hf = hourFractionInTz(renderTz, ref)
      const h = Math.floor(hf)
      const m = Math.round((hf - h) * 60)
      return DAY_INDEXES.map((i) => ({ job, day: i, hour: h, minute: m }))
    }

    let anchorSec = 0
    const anchorTs = job.last_run_ts || job.created_ts
    if (anchorTs) {
      const hf = hourFractionInTz(renderTz, new Date(anchorTs * 1000))
      anchorSec = Math.floor(hf * 3600)
    }
    const offset = anchorSec % secs

    // Sub-hour intervals: one dot per hour cell (job fires within that hour)
    if (secs < 3600) {
      const result: Slot[] = []
      for (let h = 0; h < 24; h++) {
        for (let d = 0; d < 7; d++) {
          const hourStart = h * 3600
          let firstFire = offset
          while (firstFire < hourStart) firstFire += secs
          if (firstFire < hourStart + 3600) {
            const m = Math.floor((firstFire % 3600) / 60)
            result.push({ job, day: d, hour: h, minute: m })
          }
        }
      }
      return result
    }

    // Hourly or longer: show each fire time
    const result: Slot[] = []
    for (let t = offset; t < 86400; t += secs) {
      const h = Math.floor(t / 3600)
      const m = Math.floor((t % 3600) / 60)
      for (let d = 0; d < 7; d++) result.push({ job, day: d, hour: h, minute: m })
    }
    return result
  }

  // Cron expression: use raw cron_expr field
  const expr = job.cron_expr || ''
  const parts = expr.split(/\s+/)
  if (parts.length !== 5) return []

  const [minF, hourF, , , dowF] = parts
  const minutes = parseField(minF, 0, 59)
  const hours = parseField(hourF, 0, 23)
  const rawDows = parseField(dowF, 0, 7)
  const dows = [...new Set(rawDows.map(d => d === 7 ? 0 : d))]

  // Source TZ: each job's stored timezone. Legacy jobs without a TZ are
  // treated as UTC (the default when no timezone is set).
  const srcTz = job.timezone || 'UTC'

  const slots: Slot[] = []
  for (const dow of dows) {
    for (const h of hours) {
      for (const m of minutes) {
        const converted = convertCronTime(dow, h, m, srcTz, renderTz)
        const gridDay = CRON_TO_GRID[converted.dow]
        if (gridDay === undefined) continue
        slots.push({ job, day: gridDay, hour: converted.hour, minute: converted.minute })
      }
    }
  }
  return slots
}

/** Parse a single cron field (supports *, ranges, lists, steps). */
function parseField(field: string, min: number, max: number): number[] {
  if (field === '*') return range(min, max)
  const vals = new Set<number>()
  for (const part of field.split(',')) {
    const stepMatch = part.match(/^(.+)\/(\d+)$/)
    if (stepMatch) {
      const base = stepMatch[1] === '*' ? range(min, max) : expandRange(stepMatch[1], min, max)
      const step = parseInt(stepMatch[2])
      base.filter((_, i) => i % step === 0).forEach(v => vals.add(v))
    } else {
      expandRange(part, min, max).forEach(v => vals.add(v))
    }
  }
  return [...vals].sort((a, b) => a - b)
}

function expandRange(s: string, min: number, max: number): number[] {
  const m = s.match(/^(\d+)-(\d+)$/)
  if (m) return range(parseInt(m[1]), parseInt(m[2]))
  const n = parseInt(s)
  return !isNaN(n) && n >= min && n <= max ? [n] : []
}

function range(a: number, b: number): number[] {
  const r: number[] = []
  for (let i = a; i <= b; i++) r.push(i)
  return r
}

const COLORS = [
  'bg-accent', 'bg-ok', 'bg-warn', 'bg-[var(--aim)]',
  'bg-[#6366f1]', 'bg-[#ec4899]', 'bg-[#14b8a6]', 'bg-[#f97316]',
]

interface Props {
  jobs: CronJob[]
  selectedId?: string
  onSelect: (job: CronJob) => void
  /** IANA timezone to render the grid in. Defaults to the browser's local TZ. */
  renderTz?: string
}

export default function WeekGrid({ jobs, selectedId, onSelect, renderTz }: Props) {
  const tz = renderTz || Intl.DateTimeFormat().resolvedOptions().timeZone
  const slots = useMemo(() => {
    const all: (Slot & { color: string })[] = []
    jobs.forEach((j, idx) => {
      const color = COLORS[idx % COLORS.length]
      parseCronSlots(j, tz).forEach(s => all.push({ ...s, color }))
    })
    return all
  }, [jobs, tz])

  // Group slots by day+hour for rendering
  const grid = useMemo(() => {
    const map = new Map<string, (Slot & { color: string })[]>()
    for (const s of slots) {
      const key = `${s.day}-${s.hour}`
      const arr = map.get(key) || []
      arr.push(s)
      map.set(key, arr)
    }
    return map
  }, [slots])

  const hours = range(0, 23)
  const START_HOUR = 0
  const END_HOUR = 23

  const todayCronDow = todayCronDowInTz(tz)
  const todayGrid = CRON_TO_GRID[todayCronDow]

  // Compute current week's Mon-Sun dates in the render TZ
  const weekDates = useMemo(() => {
    const now = new Date()
    const todayDow = todayCronDowInTz(tz, now)
    const jsDow = todayDow // already 0=Sun..6=Sat
    const mondayOffset = jsDow === 0 ? -6 : 1 - jsDow
    const parts = new Intl.DateTimeFormat('en-US', {
      timeZone: tz,
      year: 'numeric', month: '2-digit', day: '2-digit',
    }).formatToParts(now)
    const y = parseInt(parts.find(p => p.type === 'year')?.value || '2026')
    const m = parseInt(parts.find(p => p.type === 'month')?.value || '1')
    const d = parseInt(parts.find(p => p.type === 'day')?.value || '1')
    const monday = new Date(Date.UTC(y, m - 1, d))
    monday.setUTCDate(monday.getUTCDate() + mondayOffset)
    return DAY_INDEXES.map((i) => {
      const dt = new Date(monday)
      dt.setUTCDate(monday.getUTCDate() + i)
      // Localized day/month: a hardcoded MM/DD under a now-translated weekday
      // read as "Mo. 07/30" in de, where the locale writes 30.07.
      return fmtDateFields(dt, { month: '2-digit', day: '2-digit', timeZone: 'UTC' })
    })
  }, [tz])

  // Now-line: fractional hour in render TZ. Tick every 60s so the line moves.
  const [nowHour, setNowHour] = useState(() => hourFractionInTz(tz))
  useEffect(() => {
    setNowHour(hourFractionInTz(tz))
    const update = () => setNowHour(hourFractionInTz(tz))
    const id = setInterval(update, 60_000)
    return () => clearInterval(id)
  }, [tz])

  // Now-line offset within its hour cell (0-1)
  const nowInRange = nowHour >= START_HOUR && nowHour < END_HOUR + 1
  const nowRowIdx = Math.floor(nowHour) - START_HOUR // which hour row
  const nowFrac = nowHour - Math.floor(nowHour) // fraction within that hour

  return (
    <div className="overflow-x-auto">
      <div className="grid min-w-[600px]" style={{ gridTemplateColumns: '50px repeat(7, 1fr)' }}>
        {/* Header row */}
        <div className="py-2" />
        {DAY_INDEXES.map((i) => (
          <div key={i} className={`py-2 text-center border-l border-l-border/20 ${i === todayGrid ? 'text-accent' : 'text-muted'}`}>
            <div className="text-[11px] font-medium">{dayLabel(i)}</div>
            <div className="text-[10px]">{weekDates[i]}</div>
          </div>
        ))}

        {/* Time rows */}
        {hours.map((h, hi) => (
          <div key={h} className="contents">
            <div className="text-[11px] text-muted pr-2 text-right py-1 border-t border-border/30 relative overflow-visible">
              {h.toString().padStart(2, '0')}:00
            </div>
            {DAY_INDEXES.map((dayIdx) => {
              const cellSlots = grid.get(`${dayIdx}-${h}`) || []
              const isNowRow = nowInRange && hi === nowRowIdx
              return (
                <div
                  key={dayIdx}
                  className={`border-t border-border/30 border-l border-l-border/20 py-1 px-0.5 min-h-[28px] flex items-center justify-center gap-1 flex-wrap relative ${dayIdx === todayGrid ? 'bg-accent/5' : ''}`}
                >
                  {cellSlots.map((s, si) => (
                    <button
                      key={si}
                      className={`w-2.5 h-2.5 rounded-full ${s.color} cursor-pointer hover:scale-150 transition-transform ${!s.job.enabled ? 'opacity-30' : ''} ${selectedId === s.job.id ? 'ring-2 ring-accent ring-offset-1 ring-offset-bg' : ''}`}
                      title={`${s.job.name}${!s.job.enabled ? ' (paused)' : ''} — ${s.hour.toString().padStart(2,'0')}:${s.minute.toString().padStart(2,'0')} ${tz}`}
                      aria-label={`${s.job.name} at ${s.hour.toString().padStart(2,'0')}:${s.minute.toString().padStart(2,'0')} ${tz}`}
                      onClick={() => onSelect(s.job)}
                    />
                  ))}
                  {isNowRow && (
                    <div className="absolute left-0 right-0 pointer-events-none z-10" style={{ top: `${nowFrac * 100}%` }}>
                      <div className="h-[1.5px] bg-danger w-full" />
                    </div>
                  )}
                </div>
              )
            })}
          </div>
        ))}
      </div>

      {/* Legend */}
      {jobs.length > 0 && (
        <div className="flex gap-3 mt-3 flex-wrap">
          {jobs.map((j, idx) => (
            <button
              key={j.id}
              className={`flex items-center gap-1.5 text-[12px] cursor-pointer hover:text-text transition-colors ${selectedId === j.id ? 'text-text font-medium' : 'text-muted'}`}
              onClick={() => onSelect(j)}
            >
              <span className={`w-2.5 h-2.5 rounded-full ${COLORS[idx % COLORS.length]} ${!j.enabled ? 'opacity-30' : ''}`} />
              {j.name}{!j.enabled ? ' (paused)' : ''}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
