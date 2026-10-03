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
}: Pick<PromptStashState, 'error' | 'notice' | 'noticeTone' | 'count' | 'full'> & {
  chordLabel: string
  /** The host's touch signal (the one the hook and menu already take): the
   *  count's hint then points at the ⋯ menu, not at a chord a touch keyboard
   *  cannot press. */
  touch?: boolean
}) {
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
        <div data-testid="prompt-stash-status-line" className="flex min-w-0 max-w-full items-start justify-between gap-2 px-2.5 pt-1 text-left">
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
              className={`block min-w-0 max-w-full flex-1 break-words text-[12px] ${noticeTone === 'ok' ? 'text-text' : 'text-warn'}`}
            >
              {notice}
            </span>
          ) : <span aria-hidden="true" />}
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
