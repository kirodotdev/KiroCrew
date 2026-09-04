import { memo } from 'react'
import { Loader2, Play } from 'lucide-react'

import { i18nT } from '../../i18n/t'
import { useLanguageGeneration } from '../../i18n/useLanguageGeneration'

export interface ErrorCardProps {
  /** Server- or client-authored error prose, rendered verbatim. */
  content: string
  /**
   * Continue handler. Passed ONLY for the newest error row of a slot whose last
   * turn ended without a reply — a historical error further up the transcript is
   * settled and must not offer to resume anything.
   */
  onContinue?: () => void
  /** True while a continue request is in flight, so the press cannot double-fire. */
  continuing?: boolean
}

/**
 * The error row in a chat transcript.
 *
 * Every one of these carries prose that already tells the reader to retry
 * ("⟳ Connection lost — please retry."), but until now there was nothing to
 * click: recovering meant retyping the prompt. When the turn is genuinely
 * resumable the card grows an action, so the instruction and the affordance sit
 * in the same place.
 *
 * The button is deliberately absent rather than disabled when the turn is not
 * resumable — a permanently greyed control on a red card reads as a broken
 * feature, and there is no state the user could reach that would enable it.
 */
export const ErrorCard = memo(function ErrorCard({ content, onContinue, continuing }: ErrorCardProps) {
  useLanguageGeneration() // memo() bails out of the provider-level repaint; subscribe directly
  if (!onContinue) {
    return (
      <div
        className="bg-danger-subtle text-danger text-[13px] leading-5 px-3 py-2 rounded-md ring-1 ring-inset forced-colors:border ring-danger/15 self-center animate-scale-in"
        data-testid="error-card"
      >
        {content}
      </div>
    )
  }
  return (
    <div
      className="bg-danger-subtle ring-1 ring-inset forced-colors:border ring-danger/20 rounded-md self-center w-full max-w-full min-w-0 px-3 py-2 flex items-center gap-3 animate-scale-in"
      data-testid="error-card"
      data-continuable="true"
    >
      <div className="text-danger text-[13px] leading-5 flex-1 min-w-0" style={{ overflowWrap: 'anywhere' }}>
        {content}
      </div>
      <button
        type="button"
        onClick={onContinue}
        disabled={continuing}
        className="shrink-0 inline-flex items-center gap-2 text-[12px] leading-5 font-medium px-3 py-1 rounded-md bg-accent text-accent-fg border-none cursor-pointer hover:bg-accent-hover disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
        title={i18nT('pages.chat.errorCard.continue_hint')}
        data-testid="error-card-continue"
      >
        {continuing
          ? <Loader2 size={12} className="lucide-inline shrink-0 animate-spin" aria-hidden="true" />
          : <Play size={12} className="lucide-inline shrink-0" aria-hidden="true" />}
        {i18nT('pages.chat.errorCard.continue')}
      </button>
    </div>
  )
})
