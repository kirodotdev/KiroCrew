import { useState } from 'react'
import { Bell, X } from 'lucide-react'
import { i18nT } from '../../i18n/t'
import Card from './Card'
import { parseReminder } from './reminderParse'
import { sortedReminders, labelFor, repeatLabel } from './reminders'
import type { RemindersPayload } from './types'


export default function RemindersSection({ rem, remError, onAdd, onSkip, onRemove }: {
  rem: RemindersPayload | null
  remError: string | null
  /** Resolves false when the write failed, so the draft is not thrown away. */
  onAdd: (text: string, fireAt: string, everyMinutes?: number) => Promise<boolean>
  onSkip: (id: string) => void
  onRemove: (id: string) => void
}) {
  const [draft, setDraft] = useState('')
  const [addNote, setAddNote] = useState<string | null>(null)

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    const raw = draft.trim()
    if (!raw) return
    const parsed = parseReminder(raw, new Date(), i18nT('apps.crewCompanion.reminders.default_text'))
    if (parsed.needsSchedule || !parsed.fireAt) {
      // Same rule as the panel: never invent a time.
      setAddNote(i18nT('apps.crewCompanion.reminders.needs_time'))
      return
    }
    /*
      Clear the draft ONLY once the write landed: never discard the user's input
      on an unconfirmed write. Same rule as the failure notice and the
      custom-interval field.
    */
    const ok = await onAdd(parsed.text, parsed.fireAt, parsed.recurrence?.everyMinutes)
    if (!ok) return
    setDraft('')
    setAddNote(null)
  }

  const scheduled = rem ? rem.reminders.filter((r) => !r.done).length : 0
  const now = new Date()

  return (
    <Card
      title={i18nT('apps.crewCompanion.reminders.title')}
      icon={Bell}
      right={rem ? <span className="cc-muted">{i18nT('apps.crewCompanion.reminders.scheduled_count', { count: scheduled })}</span> : undefined}
    >
      {/* Add box first: this page is for editing, not only reading. */}
      <form className="cc-add" onSubmit={submit}>
        <input
          className="cc-add-input"
          value={draft}
          placeholder={i18nT('apps.crewCompanion.reminders.add_placeholder')}
          aria-label={i18nT('apps.crewCompanion.reminders.add_aria')}
          disabled={!rem}
          onChange={(e) => { setDraft(e.target.value); setAddNote(null) }}
        />
        <button type="submit" className="cc-btn" disabled={!draft.trim() || !rem}>
          {i18nT('apps.crewCompanion.reminders.add_button')}
        </button>
      </form>
      {addNote ? <div className="cc-hint">{addNote}</div> : null}

      {remError ? (
        <div className="cc-muted">{i18nT('apps.crewCompanion.reminders.offline')}</div>
      ) : rem === null ? (
        <div className="cc-muted">{i18nT('apps.crewCompanion.reminders.loading')}</div>
      ) : rem.reminders.length === 0 ? (
        <div className="cc-muted">{i18nT('apps.crewCompanion.reminders.empty')}</div>
      ) : (
        <div>
          {sortedReminders(rem.reminders).map((r, i) => {
            const l = labelFor(r.fireAt, now)
            const tag = r.done
              ? i18nT('apps.crewCompanion.reminders.tag_done')
              : r.recurrence ? repeatLabel(r.recurrence.everyMinutes)
              : (l.absLabel ? l.relLabel : '')
            return (
              <div key={r.id} className={`cc-row${i === 0 ? ' is-first' : ''}`}>
                <span className={`cc-rem-when${r.done ? ' cc-rem-done' : ''}`}>{l.absLabel ?? l.relLabel}</span>
                <span className={`cc-rem-text${r.done ? ' cc-rem-done' : ''}`}>{r.text}</span>
                <span className="cc-rem-tag">{tag}</span>
                {/* Skip only where there is a next occurrence to move to. */}
                {r.recurrence && !r.done ? (
                  <button
                    type="button"
                    className="cc-icon-btn"
                    title={i18nT('apps.crewCompanion.reminders.skip_title')}
                    aria-label={i18nT('apps.crewCompanion.reminders.skip_aria', { text: r.text })}
                    onClick={() => onSkip(r.id)}
                  >
                    {i18nT('apps.crewCompanion.reminders.skip')}
                  </button>
                ) : null}
                <button
                  type="button"
                  className="cc-icon-btn is-remove"
                  title={i18nT('apps.crewCompanion.reminders.remove_title')}
                  aria-label={i18nT('apps.crewCompanion.reminders.remove_aria', { text: r.text })}
                  onClick={() => onRemove(r.id)}
                >
                  <X className="lucide-inline" aria-hidden />
                </button>
              </div>
            )
          })}
        </div>
      )}
    </Card>
  )
}
