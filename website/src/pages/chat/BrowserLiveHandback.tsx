import { useId, useState, type FormEvent, type KeyboardEvent } from 'react'
import { SendHorizontal } from 'lucide-react'

import { Btn } from '../../components/ui'
import { i18nT } from '../../i18n/t'
import { sanitizeLlmOutput } from '../../utils/sanitize'
import { HANDBACK_NOTE_MAX } from '../../hooks/useBrowserLive'
import { useImeGuard } from '../../hooks/useImeGuard'
import type { BrowserHandbackSaid } from '../../api/client/browserLive'

/**
 * The hand-back bar: shown while the person holds the browser, so they can tell
 * the agent what they did as they give it back ("I signed in", "Didn't work",
 * a sentence of their own), and the agent acts on it at once instead of
 * looking again or asking.
 *
 * The choices are the ones the agent's waiting op offered, else the card's own
 * four. A choice is picked (one at a time, again to unpick) and goes with the
 * hand-back; the note field sends on Enter. Esc in the field is a plain hand
 * back with nothing said, the same as Esc on the picture.
 *
 * The bar sits OUTSIDE the canvas: the canvas forwards only the keys that reach
 * its own element, so nothing typed here ever goes to the page.
 */

/** The card's own choices when the agent offered none. */
export const DEFAULT_HANDBACK_CHOICES = [
  'pages.chat.browserLiveHandback.choice_done',
  'pages.chat.browserLiveHandback.choice_didnt_work',
  'pages.chat.browserLiveHandback.choice_wrong_site',
  'pages.chat.browserLiveHandback.choice_stop',
] as const

export function handbackChoices(offered: readonly string[]): string[] {
  if (offered.length) return offered.map(o => sanitizeLlmOutput(o).slice(0, 40)).filter(Boolean).slice(0, 4)
  return DEFAULT_HANDBACK_CHOICES.map(key => i18nT(key))
}

export function BrowserLiveHandbackBar({ offered, onHandBack, draft, className }: {
  /** The agent's own choices (`handback_options`); empty for the defaults. */
  offered: readonly string[]
  onHandBack: (said?: BrowserHandbackSaid) => void
  /** What a hand-back that failed to send said, shown again rather than lost. */
  draft?: BrowserHandbackSaid
  className?: string
}) {
  const choices = handbackChoices(offered)
  const [choice, setChoice] = useState<string | null>(draft?.choice ?? null)
  const [note, setNote] = useState(draft?.note ?? '')
  const noteId = useId()
  const ime = useImeGuard()
  const send = () => {
    const said: BrowserHandbackSaid = {}
    if (choice) said.choice = choice
    if (note.trim()) said.note = note.trim()
    onHandBack(said.choice || said.note ? said : undefined)
  }
  const onSubmit = (e: FormEvent) => { e.preventDefault(); send() }
  const onKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    // Keys typed here are the person's note, never the page's; Esc is the plain
    // hand back the picture's Esc is. An Enter or Esc the IME is using to commit
    // or cancel a candidate belongs to the composition, not to the hand-back.
    e.stopPropagation()
    if (e.key === 'Enter') {
      if (ime.claimEnter(e)) send()
      return
    }
    if (e.key === 'Escape' && ime.claimKey(e)) { e.preventDefault(); onHandBack(undefined) }
  }
  return (
    <form
      className={`flex flex-col gap-1.5 px-3 py-2 border-t border-border bg-bg-elevated ${className ?? ''}`}
      onSubmit={onSubmit}
      data-testid="browser-live-handback"
    >
      <div role="group" aria-label={i18nT('pages.chat.browserLiveHandback.what_happened')} className="flex flex-wrap items-center gap-1.5">
        {choices.map(c => (
          <button
            key={c}
            type="button"
            aria-pressed={choice === c}
            onClick={() => setChoice(prev => (prev === c ? null : c))}
            className={`rounded-full border px-2.5 py-0.5 text-[12px] cursor-pointer transition-colors ${
              choice === c ? 'border-accent bg-accent-subtle text-accent font-semibold' : 'border-border bg-bg text-text hover:bg-bg-hover'
            }`}
            data-testid="browser-live-handback-choice"
          >
            {c}
          </button>
        ))}
      </div>
      <div className="flex items-center gap-2">
        <label htmlFor={noteId} className="sr-only">{i18nT('pages.chat.browserLiveHandback.note_label')}</label>
        <input
          id={noteId}
          type="text"
          value={note}
          maxLength={HANDBACK_NOTE_MAX}
          onChange={e => setNote(e.target.value)}
          {...ime.bindComposition<HTMLInputElement>({})}
          onKeyDown={onKeyDown}
          placeholder={i18nT('pages.chat.browserLiveHandback.note_placeholder')}
          aria-label={i18nT('pages.chat.browserLiveHandback.note_label')}
          autoComplete="off"
          className="min-w-0 flex-1 rounded-md border border-border bg-bg px-2.5 py-1 text-[13px] text-text placeholder:text-muted focus:outline-none focus:border-accent"
          data-testid="browser-live-handback-note"
        />
        <Btn
          primary
          type="submit"
          className="!px-2 shrink-0"
          aria-label={i18nT('pages.chat.browserLiveHandback.send')}
          title={i18nT('pages.chat.browserLiveHandback.send')}
          data-testid="browser-live-handback-send"
        >
          <SendHorizontal size={14} aria-hidden="true" />
        </Btn>
      </div>
    </form>
  )
}
