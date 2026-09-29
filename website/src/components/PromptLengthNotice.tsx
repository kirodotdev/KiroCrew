import { memo, useDeferredValue, useMemo } from 'react'
import { AlertTriangle } from 'lucide-react'
import { i18nT } from '../i18n/t'
import { fmtCompact, fmtPercent } from '../i18n/format'
import { useLanguageGeneration } from '../i18n/useLanguageGeneration'
import type { PasteBlock } from '../utils/pasteTokens'
import { checkPromptLength, measurePrompt, sentPromptText, type PromptLengthCheck } from './composerPromptLength'

/** Visible line for a prompt near or over its limit; '' when under. */
export function promptLengthMessage(check: PromptLengthCheck): string {
  if (check.level === 'ok') return ''
  return check.level === 'over'
    ? i18nT('components.promptLength.over_tokens', { over: fmtCompact(check.overBy), limit: fmtCompact(check.limit) })
    : i18nT('components.promptLength.near_tokens', {
        used: fmtCompact(check.used),
        limit: fmtCompact(check.limit),
        pct: fmtPercent(Math.min(check.ratio, 1)),
      })
}

interface Props {
  /** Editor value, with paste chips still collapsed. */
  value: string
  /** Paste blocks backing the chips in `value`. */
  blocks: readonly PasteBlock[]
  /** The active model's context window in tokens; 0/undefined = unknown. */
  contextWindowTokens?: number
}

/**
 * Composer strip that appears only when the prompt about to be sent is near
 * (>= 90%) or over the limit it will meet. Renders nothing visible otherwise.
 *
 * The screen-reader announcement is a separate polite live region whose text
 * changes only when the level changes, so typing inside one level announces
 * nothing and the numbers in the visible line never flood the reader. Neither
 * element is focusable.
 */
function PromptLengthNoticeImpl({ value, blocks, contextWindowTokens }: Props) {
  useLanguageGeneration() // memo() bails out of the provider-level repaint; subscribe directly
  // A multi-megabyte paste is measured off the typing path.
  const deferredValue = useDeferredValue(value)
  const check = useMemo(
    () => checkPromptLength(measurePrompt(sentPromptText(deferredValue, blocks)), contextWindowTokens),
    [deferredValue, blocks, contextWindowTokens],
  )
  const announce = check.level === 'over'
    ? i18nT('components.promptLength.sr_over')
    : check.level === 'near'
      ? i18nT('components.promptLength.sr_near')
      : ''
  const message = promptLengthMessage(check)
  return (
    <>
      <span className="sr-only" aria-live="polite" aria-atomic="true" data-testid="prompt-length-live">
        {announce}
      </span>
      {message && (
        <div
          data-testid="prompt-length-notice"
          data-level={check.level}
          className={`flex items-start gap-1.5 px-3 pb-1 text-[11px] leading-snug ${check.level === 'over' ? 'text-danger' : 'text-warn'}`}
        >
          <AlertTriangle size={12} className="shrink-0 mt-[1px]" aria-hidden="true" />
          <span>{message}</span>
        </div>
      )}
    </>
  )
}

const PromptLengthNotice = memo(PromptLengthNoticeImpl)
export default PromptLengthNotice
