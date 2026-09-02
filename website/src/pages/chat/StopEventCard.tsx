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
