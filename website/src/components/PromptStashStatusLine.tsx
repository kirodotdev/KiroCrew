import { useEffect, useState } from 'react'
import { i18nT } from '../i18n/t'
import type { PromptStashState } from '../hooks/usePromptStash'
import ErrorNotice from './ErrorNotice'

/** A short status line directly under the draft it describes. */
export default function PromptStashStatusLine({
  error = '',
  notice = '',
  noticeTone = 'warn',
  count,
  full,
  chordLabel,
  touch = false,
  reclaimCount = 0,
  onClearOtherSessions,
}: Pick<PromptStashState, 'error' | 'notice' | 'noticeTone' | 'count' | 'full'> & {
  /** Entries other sessions hold, offered after a refusal at the byte budget. */
  reclaimCount?: number
  /** Deletes those entries. Called only from the confirm step. */
  onClearOtherSessions?: () => void
  chordLabel: string
  /** The host's touch signal (the one the hook and menu already take): the
   *  count's hint then points at the ⋯ menu, not at a chord a touch keyboard
   *  cannot press. */
  touch?: boolean
}) {
  // Two steps: the first press names the count and asks again, so a stray
  // click cannot delete drafts the user kept in other sessions.
  const [confirming, setConfirming] = useState(false)
  useEffect(() => { if (reclaimCount === 0) setConfirming(false) }, [reclaimCount])
  const reclaim = Boolean(notice) && reclaimCount > 0 && onClearOtherSessions !== undefined
  const hint = full
    ? (touch
      ? i18nT('components.promptStash.badge_full_hint_touch')
      : i18nT('components.promptStash.badge_full_hint', { chord: chordLabel }))
    : (touch
      ? i18nT('components.promptStash.badge_hint_touch')
      : i18nT('components.promptStash.badge_hint', { chord: chordLabel }))
  return (
    <>
      {(error || notice || count > 0) && (
        // With the clear offer showing, the row wraps: the notice takes a full line and the offer and count sit under it, so a
        // 320px composer never has to fit three intrinsic widths on one line (ChatInput's wrapper clips overflow).
        <div data-testid="prompt-stash-status-line" className={`flex min-w-0 max-w-full items-start justify-between gap-2 px-2.5 pt-1 text-left ${reclaim ? 'flex-wrap gap-y-0.5' : ''}`}>
          {error ? (
            <>
              {/* No hand-off: the unsent composer draft this stash failed to save is still only in the composer, and the hand-off would unmount it.
                  Wraps rather than truncates: this is the one message whose second half ("Your draft is still in the composer") is the recovery, and a
                  one-line clamp cuts exactly that half first at phone widths. The status line sits on its own row above the buttons, so wrapping
                  grows the row instead of pushing the action row off-screen; the short notices beside it keep their one-line bound. */}
              <ErrorNotice
                variant="inline"
                message={error}
                testId="prompt-stash-error"
                className="w-full min-w-0 max-w-full"
              />
            </>
          ) : notice ? (
            <span
              aria-hidden="true"
              data-testid="prompt-stash-notice"
              className={`block min-w-0 max-w-full flex-1 break-words text-[12px] ${reclaim ? 'basis-full' : ''} ${noticeTone === 'ok' ? 'text-text' : 'text-warn'}`}
            >
              {notice}
            </span>
          ) : <span aria-hidden="true" />}
          {reclaim && (
            <button
              type="button"
              data-testid={confirming ? 'prompt-stash-clear-others-confirm' : 'prompt-stash-clear-others'}
              onClick={() => {
                if (!confirming) { setConfirming(true); return }
                setConfirming(false)
                onClearOtherSessions()
              }}
              className={`min-w-0 max-w-full flex-1 break-words rounded px-1.5 text-left text-[12px] underline underline-offset-2 focus-visible:outline focus-visible:outline-2 ${confirming ? 'text-danger' : 'text-text'}`}
            >
              {confirming
                ? i18nT('components.promptStash.clear_others_confirm', { count: reclaimCount })
                : i18nT('components.promptStash.clear_others', { count: reclaimCount })}
            </button>
          )}
          {count > 0 && (
            <span
              data-testid="prompt-stash-count"
              title={hint}
              className={`shrink-0 select-none text-[12px] tabular-nums ${full ? 'text-warn' : 'text-muted'}`}
            >
              {i18nT('components.promptStash.badge', { count })}
              <span className="sr-only">. {hint}</span>
            </span>
          )}
        </div>
      )}
    </>
  )
}
