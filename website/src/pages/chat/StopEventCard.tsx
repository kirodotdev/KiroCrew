import { memo } from 'react'
import { motion } from 'framer-motion'
import { Square, XOctagon } from 'lucide-react'
import type { ChatMessage } from '../../types'

import { i18nT } from '../../i18n/t'
import { useLanguageGeneration } from '../../i18n/useLanguageGeneration'
/** Inline card for stop_event messages. Three visual states driven by meta.state. */
export default memo(function StopEventCard({ message }: { message: ChatMessage }) {
  useLanguageGeneration() // memo() bails out of the provider-level repaint; subscribe directly
  const state = (message.meta?.state as string) ?? 'stopping'

  if (state === 'stopping') {
    return (
      <motion.div
        role="status"
        aria-label={i18nT('pages.chat.stopEventCard.stopping_in_progress')}
        aria-live="polite"
        className="text-danger text-[13px] leading-5 font-mono px-3 py-2 rounded-md bg-danger-subtle inline-flex items-center gap-2"
        animate={{ opacity: [0.6, 1, 0.6] }}
        transition={{ duration: 1.2, repeat: Infinity }}
        data-testid="stop-event-card"
        data-state={state}
      >
        <Square size={13} fill="currentColor" className="lucide-inline" aria-hidden="true" />
        {i18nT('pages.chat.stopEventCard.stopping')}
      </motion.div>
    )
  }

  if (state === 'stop_failed_reset') {
    // Transcript row, deliberately NOT routed through `ErrorNotice`, on the same
    // grounds as `ErrorCard`'s exemption in the `errors-use-error-notice` rule:
    // the agent is already in this conversation, so an "ask the agent" hand-off
    // would be circular, and the text is a fixed localized status label rather
    // than a backend error body — there is no journal entry for it to recover.
    // The class recipe below IS the transcript's error-row treatment: it is
    // pinned byte-for-byte to `ErrorCard`'s non-continuable card (#6229,
    // `AppSdkStopEventCardParity.test.tsx`), so the two cannot drift apart.
    return (
      <div
        role="alert"
        aria-label={i18nT('pages.chat.stopEventCard.stop_failed_session_reset')}
        className="text-danger text-[13px] leading-5 font-mono px-3 py-2 rounded-md ring-1 ring-inset forced-colors:border ring-danger/15 bg-danger-subtle inline-flex items-center gap-2"
        data-testid="stop-event-card"
        data-state={state}
      >
        <XOctagon size={13} className="lucide-inline" aria-hidden="true" />
        {i18nT('pages.chat.stopEventCard.stop_failed_session_reset_2')}
      </div>
    )
  }

  // Default: 'stopped'
  return (
    <div
      role="status"
      aria-label={i18nT('pages.chat.stopEventCard.stopped')}
      className="text-danger text-[13px] leading-5 font-mono px-3 py-2 rounded-md bg-danger-subtle inline-flex items-center gap-2"
      data-testid="stop-event-card"
      data-state={state}
    >
      <Square size={13} fill="currentColor" className="lucide-inline" aria-hidden="true" />
      {i18nT('pages.chat.stopEventCard.stopped_2')}
    </div>
  )
})
