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
  // The trigger the editor was opened from. Closing unmounts the focused
  // input and a browser then drops focus to document.body, so Enter and
  // Escape hand focus back to the trigger (the ARIA edit-in-place pattern),
  // following SessionTitleControl. A pointer blur saves but moves nothing:
  // redirecting focus off the element the user clicked would be the
  // unexpected context change WCAG 3.2.2 forbids.
  const triggerRef = useRef<HTMLButtonElement | null>(null)
  const restoreFocus = () => requestAnimationFrame(() => triggerRef.current?.focus({ preventScroll: true }))
  const close = (restore = false) => { setDraft(null); setError(null); if (restore) restoreFocus() }
  // restore=true for a keyboard close (Enter/Escape); a pointer blur passes false (see triggerRef above).
  const save = (restore = false) => {
    if (draft === null || busy.current) return
    const next = draft.trim()
    if (!next || next === title) return close(restore)
    // Close only if the user has not typed on while this save was in flight.
    busy.current = true
    onRename(next).then(
      () => {
        setError(null)
        setDraft(d => {
          if (d !== null && d.trim() !== next) return d
          if (restore) restoreFocus()
          return null
        })
      },
      () => setError(i18nT('apps.meetings.session.renameFailed')),
    ).finally(() => { busy.current = false })
  }

  if (draft === null) {
    return (
      <h2 className="text-lg font-semibold text-text-strong min-w-0">
        <button
          ref={triggerRef}
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
        {...ime.bindEnter({ onEnter: () => save(true), onEscape: () => close(true), onBlur: () => save(false) })}
      />
      {/* No hand-off: the unsaved title draft is still in the input above. */}
      <ErrorNotice message={error} />
    </div>
  )
}
