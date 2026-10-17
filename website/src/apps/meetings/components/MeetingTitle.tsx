// The meeting title, renamed in place: Enter or blur saves, Escape cancels.
// A failed save keeps the draft open with an error.

import { useRef, useState } from 'react'
import { Pen } from 'lucide-react'

import { i18nT } from '../../../i18n/t'
import ErrorNotice from '../../../components/ErrorNotice'
import { Input } from '../../../components/ui'
import { useImeGuard } from '../../../hooks/useImeGuard'

export default function MeetingTitle({ title, onRename }: {
  title: string
  onRename: (title: string) => Promise<unknown>
}) {
  const ime = useImeGuard()
  const [draft, setDraft] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  // One save at a time, so two renames can never land out of order.
  const busy = useRef(false)
  const close = () => { setDraft(null); setError(null) }
  const save = () => {
    if (draft === null || busy.current) return
    const next = draft.trim()
    if (!next || next === title) return close()
    // Close only if the user has not typed on while this save was in flight.
    busy.current = true
    onRename(next).then(
      () => { setError(null); setDraft(d => (d !== null && d.trim() !== next ? d : null)) },
      () => setError(i18nT('apps.meetings.session.renameFailed')),
    ).finally(() => { busy.current = false })
  }

  if (draft === null) {
    return (
      <h2 className="text-lg font-semibold text-text-strong min-w-0">
        <button
          type="button"
          onClick={() => setDraft(title)}
          className="cursor-text flex max-w-full items-center gap-1.5 rounded-md px-1.5 hover:bg-bg-hover focus-ring"
        >
          <span className="truncate">{title || i18nT('apps.meetings.session.untitled')}</span>
          <Pen size={13} aria-hidden className="shrink-0 text-muted" />
        </button>
      </h2>
    )
  }
  return (
    <div className="flex-1 min-w-0">
      <Input
        value={draft}
        className="w-full"
        onChange={e => setDraft(e.target.value)}
        autoFocus
        aria-label={i18nT('apps.meetings.meeting.titleLabel')}
        {...ime.bindEnter({ onEnter: save, onEscape: close, onBlur: save })}
      />
      {/* No hand-off: the unsaved title draft is still in the input above. */}
      <ErrorNotice message={error} />
    </div>
  )
}
