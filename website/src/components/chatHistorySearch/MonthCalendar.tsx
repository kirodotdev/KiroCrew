/** A one-month calendar that picks ONE day, for the chat history search. */
import { useMemo, useState } from 'react'
import { ChevronLeft, ChevronRight } from 'lucide-react'

import { fmtDateFields, fmtWeekday } from '../../i18n/format'
import { i18nT } from '../../i18n/t'

/** `YYYY-MM-DD` of a local date. */
export function dayKey(d: Date): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

/** The local date a `YYYY-MM-DD` key names, or null for anything else. */
export function dayDate(day: string): Date | null {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(day)
  return m ? new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3])) : null
}

export default function MonthCalendar({ value, onPick, today = new Date() }: {
  /** The picked day (`YYYY-MM-DD`), or '' for none. */
  value: string
  onPick: (day: string) => void
  today?: Date
}) {
  const start = dayDate(value) ?? today
  const [month, setMonth] = useState(() => new Date(start.getFullYear(), start.getMonth(), 1))
  const todayKey = dayKey(today)
  const cells = useMemo(() => {
    // Weeks start on Monday (ISO), so the weekday header and fmtWeekday agree.
    const lead = (month.getDay() + 6) % 7
    const days = new Date(month.getFullYear(), month.getMonth() + 1, 0).getDate()
    const out: Array<Date | null> = Array.from({ length: lead }, () => null)
    for (let d = 1; d <= days; d++) out.push(new Date(month.getFullYear(), month.getMonth(), d))
    return out
  }, [month])
  const step = (n: number) => setMonth(m => new Date(m.getFullYear(), m.getMonth() + n, 1))
  const nextIsFuture = new Date(month.getFullYear(), month.getMonth() + 1, 1) > today

  return (
    <div className="px-4 py-3" data-testid="chat-history-calendar">
      <div className="flex items-center justify-between pb-2">
        <button type="button" onClick={() => step(-1)} className="p-1.5 rounded text-muted hover:text-text hover:bg-bg-hover cursor-pointer focus-ring" aria-label={i18nT('components.chatHistorySearch.previous_month')}>
          <ChevronLeft size={16} />
        </button>
        <span className="text-sm font-medium text-text" aria-live="polite">{fmtDateFields(month, { year: 'numeric', month: 'long' })}</span>
        <button type="button" disabled={nextIsFuture} onClick={() => step(1)} className="p-1.5 rounded text-muted hover:text-text hover:bg-bg-hover cursor-pointer disabled:opacity-30 disabled:cursor-not-allowed focus-ring" aria-label={i18nT('components.chatHistorySearch.next_month')}>
          <ChevronRight size={16} />
        </button>
      </div>
      <div role="grid" aria-label={fmtDateFields(month, { year: 'numeric', month: 'long' })} className="grid grid-cols-7 gap-1 text-center text-[13px]">
        {[1, 2, 3, 4, 5, 6, 7].map(i => (
          <span key={i} role="columnheader" className="py-1 text-[11px] text-muted">{fmtWeekday(i, 'narrow')}</span>
        ))}
        {cells.map((d, i) => {
          if (!d) return <span key={`pad-${i}`} />
          const key = dayKey(d)
          const future = key > todayKey
          const picked = key === value
          return (
            <button
              key={key}
              type="button"
              role="gridcell"
              aria-selected={picked}
              aria-current={key === todayKey ? 'date' : undefined}
              aria-label={fmtDateFields(d, { year: 'numeric', month: 'long', day: 'numeric' })}
              disabled={future}
              onClick={() => onPick(key)}
              className={`h-9 rounded-full cursor-pointer focus-ring disabled:opacity-30 disabled:cursor-not-allowed ${picked ? 'bg-accent text-accent-fg' : key === todayKey ? 'text-accent font-semibold hover:bg-bg-hover' : 'text-text hover:bg-bg-hover'}`}
            >
              {d.getDate()}
            </button>
          )
        })}
      </div>
    </div>
  )
}
