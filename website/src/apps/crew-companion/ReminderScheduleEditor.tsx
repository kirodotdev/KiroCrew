import { useEffect, useRef, useState } from 'react'
import { i18nT } from '../../i18n/t'
import { fmtWeekday } from '../../i18n/format'
import SimpleSelect from '../../components/SimpleSelect'
import ErrorNotice from '../../components/ErrorNotice'
import { errorText } from './errorText'
import { repeatLabel } from './reminders'
import { REPEATS, REPEAT_MINUTES, nextFireAt, scheduleOf, type Repeat, type Schedule } from './reminderSchedule'
import type { Reminder } from './types'

export type OnReschedule = (id: string, fireAt: string, everyMinutes: number | null) => Promise<unknown>

const REPEAT_LABEL: Record<Repeat, () => string> = {
  once: () => i18nT('apps.crewCompanion.reminders.schedule_once'),
  hourly: () => i18nT('apps.crewCompanion.reminders.schedule_hourly'),
  daily: () => i18nT('apps.crewCompanion.reminders.schedule_daily'),
  weekly: () => i18nT('apps.crewCompanion.reminders.schedule_weekly'),
}
/** ISO weekdays, Monday first. */
const WEEKDAYS = ['1', '2', '3', '4', '5', '6', '7']
const pad = (n: number) => String(n).padStart(2, '0')
/** Equal in every field the repeat shows: hourly hides the hour, and only weekly shows a weekday. */
const sameSchedule = (a: Schedule, b: Schedule) => a.repeat === b.repeat && a.minute === b.minute
  && (a.repeat === 'hourly' || a.hour === b.hour) && (a.repeat !== 'weekly' || a.weekday === b.weekday)

/** A reminder row's Change schedule form: the same reminder, a hand-set repeat and time. */
export default function ReminderScheduleEditor({ r, onReschedule, onClose }: {
  r: Reminder
  onReschedule: OnReschedule
  /** After the save lands, or on Cancel. */
  onClose: () => void
}) {
  const [initial] = useState(() => scheduleOf(r))
  // The reminder's own interval, offered as a Repeat choice when no preset matches it.
  const [custom] = useState(() => (initial.repeat === 'custom' ? r.recurrence?.everyMinutes ?? null : null))
  // What the server is known to hold. A failed save clears it: the write may still have landed.
  const opened = useRef<Schedule | null>(initial)
  const [s, setS] = useState(initial)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const first = useRef<HTMLElement>(null)
  useEffect(() => { first.current?.focus() }, [])
  const set = (patch: Partial<Schedule>) => setS((x) => ({ ...x, ...patch }))
  const repeats: Schedule['repeat'][] = custom === null ? REPEATS : [...REPEATS, 'custom']
  // Nothing closes or edits the form while a save is in flight, so its completion closes this form and loses no draft.
  const cancel = () => { if (!busy) onClose() }

  const save = (e: React.FormEvent) => {
    e.preventDefault()
    const o = opened.current
    // Unchanged: saving would move the reminder off its own next fire, a skipped one included.
    if (o && sameSchedule(s, o)) return onClose()
    setBusy(true)
    const fireAt = nextFireAt(s, new Date(), new Date(Date.parse(r.fireAt)))
    onReschedule(r.id, fireAt, s.repeat === 'custom' ? custom : REPEAT_MINUTES[s.repeat]).then(onClose, (err: unknown) => {
      opened.current = null
      setError(i18nT('apps.crewCompanion.reminders.couldnt_reschedule', { error: errorText(err) }))
      setBusy(false)
    })
  }

  return (
    // Escape mirrors the Cancel button, as in the text editor; an open dropdown claims (preventDefault) its own Escape first.
    // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions -- a keyboard shortcut for the Cancel button below; every gesture inside is on a real control
    <form className="cc-rem-edit cc-rem-schedule" onSubmit={save}
      onKeyDown={(e) => { if (e.key === 'Escape' && !e.defaultPrevented) cancel() }}>
      {/* Which reminder this is: the form takes the row's text slot. */}
      <span className="cc-rem-text cc-rem-schedule-text">{r.text}</span>
      <SimpleSelect ref={first} aria-label={i18nT('apps.crewCompanion.reminders.schedule_repeat_aria')}
        options={repeats} disabled={busy}
        optionLabels={repeats.map((k) => (k === 'custom' ? repeatLabel(custom ?? 0) : REPEAT_LABEL[k]()))}
        value={s.repeat} onChange={(v) => set({ repeat: v as Schedule['repeat'] })} />
      {s.repeat === 'weekly' ? (
        <SimpleSelect aria-label={i18nT('apps.crewCompanion.reminders.schedule_weekday_aria')} disabled={busy}
          options={WEEKDAYS} optionLabels={WEEKDAYS.map((d) => fmtWeekday(Number(d), 'long'))}
          value={String(s.weekday)} onChange={(v) => set({ weekday: Number(v) })} />
      ) : null}
      {s.repeat === 'hourly' ? (
        // A visible label: a bare "0" alone does not say it is the minute past the hour.
        <label className="cc-rem-minute">
          {i18nT('apps.crewCompanion.reminders.schedule_minute_aria')}
          <input type="number" className="cc-add-input" min={0} max={59} value={s.minute} disabled={busy}
            aria-label={i18nT('apps.crewCompanion.reminders.schedule_minute_aria')}
            onChange={(e) => set({ minute: Math.min(59, Math.max(0, Math.trunc(Number(e.target.value)))) })} />
        </label>
      ) : (
        <input type="time" className="cc-add-input" value={`${pad(s.hour)}:${pad(s.minute)}`} disabled={busy}
          aria-label={i18nT('apps.crewCompanion.reminders.schedule_time_aria')}
          onChange={(e) => {
            // A cleared field keeps the last time rather than saving a guessed one.
            const [hour, minute] = e.target.value.split(':').map(Number)
            if (e.target.value) set({ hour, minute })
          }} />
      )}
      <span className="cc-rem-schedule-actions">
        <button type="submit" className="cc-btn" disabled={busy}>
          {i18nT(busy ? 'apps.crewCompanion.editor.saving' : 'apps.crewCompanion.editor.save')}</button>
        <button type="button" className="cc-btn" disabled={busy} onClick={cancel}>{i18nT('apps.crewCompanion.editor.cancel')}</button>
      </span>
      {/* No hand-off: the unsaved schedule is still in the fields above. */}
      <ErrorNotice message={error} variant="inline" />
    </form>
  )
}
