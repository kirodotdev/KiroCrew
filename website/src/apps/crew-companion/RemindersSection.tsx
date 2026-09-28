import { useCallback, useEffect, useRef, useState } from 'react'
import { Bell, MoreHorizontal, Pen, X } from 'lucide-react'
import { i18nT } from '../../i18n/t'
import Card from './Card'
import { parseReminder } from './reminderParse'
import { sortedReminders, labelFor, repeatLabel } from './reminders'
import { useImeGuard } from '../../hooks/useImeGuard'
import ErrorNotice from '../../components/ErrorNotice'
import { errorText } from './errorText'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from '../../components/ui/dropdown-menu'
import type { Reminder, RemindersPayload } from './types'

type OnEdit = (id: string, text: string) => Promise<unknown>

function ReminderRow({ r, tag, onEdit, onSkip, onRemove, hold }: {
  r: Reminder; tag: string; onEdit: OnEdit; onSkip?: () => void; onRemove: () => void
  hold: (r: Reminder, editing: boolean) => void
}) {
  const ime = useImeGuard()
  const [draft, setDraft] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  // One save at a time, so two edits can never land out of order.
  const busy = useRef(false)
  // Compared against what the editor opened with, not r.text: a poll may refresh r.text mid-edit.
  const opened = useRef('')
  const open = () => { opened.current = r.text; setDraft(r.text) }
  const editing = draft !== null
  useEffect(() => { hold(r, editing) }, [hold, r, editing])
  const close = () => { setDraft(null); setError(null) }
  const save = () => {
    if (draft === null || busy.current) return
    const next = draft.trim()
    if (!next || next === opened.current) return close()
    // Close only if the user has not typed on while this save was in flight.
    busy.current = true
    onEdit(r.id, next).then(
      () => { opened.current = next; setError(null); setDraft((d) => (d !== null && d.trim() !== next ? d : null)) },
      // A failed reply may still have saved, so the next save must not skip as "unchanged".
      (e: unknown) => { opened.current = ''; setError(i18nT('apps.issueRadar.views.crews.desk.settings_failed', { error: errorText(e) })) },
    ).finally(() => { busy.current = false })
  }

  const label = i18nT('apps.crewCompanion.reminders.edit_aria', { text: r.text })
  const more = i18nT('components.chatInput.more_actions')
  if (draft === null) {
    return (<>
      <span className={`cc-rem-text${r.done ? ' cc-rem-done' : ''}`}>{r.text}</span>
      <span className="cc-rem-tag">{tag}</span>
      {/* A row holds two actions at most, so a skippable row folds Skip and Edit into one menu. */}
      {onSkip ? (
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <button type="button" className="cc-icon-btn cc-edit" title={more} aria-label={more}>
              <MoreHorizontal className="lucide-inline" aria-hidden /></button>
          </DropdownMenuTrigger>
          {/* Focus goes to the editor, not back to the trigger, or its blur would close it. */}
          <DropdownMenuContent align="end" onCloseAutoFocus={(e) => e.preventDefault()}>
            <DropdownMenuItem onSelect={onSkip}>{i18nT('apps.crewCompanion.reminders.skip_title')}</DropdownMenuItem>
            <DropdownMenuItem onSelect={open}>{i18nT('components.commentOverlay.edit')}</DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      ) : (
        <button type="button" className="cc-icon-btn cc-edit" title={label}
          aria-label={label} onClick={open}><Pen className="lucide-inline" aria-hidden /></button>
      )}
      <button type="button" className="cc-icon-btn is-remove" title={i18nT('apps.crewCompanion.reminders.remove_title')}
        aria-label={i18nT('apps.crewCompanion.reminders.remove_aria', { text: r.text })} onClick={onRemove}>
        <X className="lucide-inline" aria-hidden /></button>
    </>)
  }
  return (
    // Remove stays hidden while editing, so the only exit beside the input is not a delete.
    <div className="cc-rem-edit">
      <input className="cc-add-input" value={draft} autoFocus aria-label={label}
        onChange={(e) => setDraft(e.target.value)}
        {...ime.bindEnter({ onEnter: save, onEscape: close, onBlur: save })} />
      {/* No hand-off: the unsaved reminder draft is still in the input above. */}
      <ErrorNotice message={error} variant="inline" />
    </div>
  )
}

export default function RemindersSection({ rem, remError, onAdd, onSkip, onRemove, onEdit }: {
  rem: RemindersPayload | null
  remError: string | null
  /** Resolves false when the write failed, so the draft is not thrown away. */
  onAdd: (text: string, fireAt: string, everyMinutes?: number) => Promise<boolean>
  onSkip: (id: string) => void
  onRemove: (id: string) => void
  onEdit: OnEdit
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
  // A reminder that fires mid-edit drops out of the next poll; keep its row until save or cancel.
  const [held, setHeld] = useState<Record<string, Reminder>>({})
  const hold = useCallback((r: Reminder, editing: boolean) => {
    setHeld((h) => {
      if (editing) return { ...h, [r.id]: r }
      if (!(r.id in h)) return h
      const next = { ...h }
      delete next[r.id]
      return next
    })
  }, [])
  const listed = rem ? sortedReminders(rem.reminders) : []
  const rows = [...listed, ...Object.values(held).filter((h) => !listed.some((x) => x.id === h.id))]
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

      {/* Beside the rows, not instead of them: a failed poll must not unmount an open edit.
          No hand-off: an unsaved reminder draft may be open in the list below. */}
      <ErrorNotice message={remError ? i18nT('apps.crewCompanion.reminders.offline') : null} variant="inline" />
      {rem === null ? (
        remError ? null : <div className="cc-muted">{i18nT('apps.crewCompanion.reminders.loading')}</div>
      ) : rows.length === 0 ? (
        <div className="cc-muted">{i18nT('apps.crewCompanion.reminders.empty')}</div>
      ) : (
        <div>
          {rows.map((r, i) => {
            const l = labelFor(r.fireAt, now)
            const tag = r.done
              ? i18nT('apps.crewCompanion.reminders.tag_done')
              : r.recurrence ? repeatLabel(r.recurrence.everyMinutes)
              : (l.absLabel ? l.relLabel : '')
            return (
              <div key={r.id} className={`cc-row cc-rem-row${i === 0 ? ' is-first' : ''}`}>
                <span className={`cc-rem-when${r.done ? ' cc-rem-done' : ''}`}>{l.absLabel ?? l.relLabel}</span>
                {/* Skip only where there is a next occurrence to move to. */}
                <ReminderRow r={r} tag={tag} onEdit={onEdit} onRemove={() => onRemove(r.id)} hold={hold}
                  onSkip={r.recurrence && !r.done ? () => onSkip(r.id) : undefined} />
              </div>
            )
          })}
        </div>
      )}
    </Card>
  )
}
