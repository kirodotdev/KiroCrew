import { memo, type ReactNode } from 'react'

import { i18nT } from '../../i18n/t'
import { useLanguageGeneration } from '../../i18n/useLanguageGeneration'
import { splitOnPlaceholder } from '../../lib/splitOnPlaceholder'
import { shortSessionKey, type SendProvenance } from './sendProvenance'

/**
 * The muted one-line "From <chat> · queued" label drawn directly
 * above a user bubble another chat sent (`session_send` / `session_broadcast`).
 *
 * Text only, on purpose: no icon or arrow glyph, no button chrome. It is a caption
 * naming the CHAT that sent the bubble below, so it cannot be read as something the
 * person typed. The sender's name opens that chat when it is still open
 * (`sessions` holds it); a closed or unknown sender is named by a short form of
 * its key, never as a link that goes nowhere. A host that knows nothing about
 * open sessions (`sessions` absent) shows the sender's title as plain text.
 */
const SendProvenanceLabel = memo(function SendProvenanceLabel({
  provenance,
  sessions,
  onOpen,
}: {
  provenance: SendProvenance
  /** Open sessions, key -> title. Absent means the host cannot tell. */
  sessions?: ReadonlyMap<string, string>
  onOpen?: (key: string) => void
}) {
  // memo() bails out of the provider-level repaint; subscribe directly.
  useLanguageGeneration()
  const { from } = provenance
  const liveTitle = sessions?.get(from)
  const isLive = liveTitle !== undefined
  const shortKey = shortSessionKey(from)

  let sender: ReactNode
  if (isLive && onOpen) {
    const name = liveTitle || provenance.title || shortKey
    sender = (
      <button
        type="button"
        onClick={() => onOpen(from)}
        className="focus-ring rounded-sm font-medium text-muted hover:text-text underline decoration-solid underline-offset-2 inline-block max-w-[7rem] sm:max-w-[12rem] truncate align-bottom"
        title={i18nT('pages.chat.sendProvenance.open_sender', { title: name })}
        data-testid="send-provenance-sender"
        data-sender-state="open"
      >
        {name}
      </button>
    )
  } else {
    // Closed (or unknown to a host that does know what is open): the short key,
    // said to be closed in the visible text -- a tooltip alone never reaches a
    // touch or keyboard reader. A host that does not know at all keeps the
    // title the row recorded.
    const name = sessions
      ? i18nT('pages.chat.sendProvenance.closed_sender_name', { name: shortKey })
      : (provenance.title || shortKey)
    const hint = sessions && provenance.title
      ? i18nT('pages.chat.sendProvenance.closed_sender', { title: provenance.title })
      : undefined
    sender = (
      <span
        className="font-medium inline-block max-w-[7rem] sm:max-w-[12rem] truncate align-bottom"
        title={hint}
        data-testid="send-provenance-sender"
        data-sender-state={sessions ? 'closed' : 'unknown'}
      >
        {name}
      </span>
    )
  }

  const sentence = provenance.via === 'session_broadcast'
    ? (provenance.count
        ? i18nT('pages.chat.sendProvenance.from_broadcast', { count: provenance.count })
        : i18nT('pages.chat.sendProvenance.from_broadcast_unknown'))
    : provenance.mode === 'steered'
      ? i18nT('pages.chat.sendProvenance.from_steered')
      : provenance.mode === 'queued'
        ? i18nT('pages.chat.sendProvenance.from_queued')
        : i18nT('pages.chat.sendProvenance.from')

  return (
    <div
      // Right-aligned with the bubble. One line at every normal width; on a very
      // narrow column the sentence wraps rather than cutting off the mode.
      className="max-w-full text-right text-[12px] leading-5 text-muted mb-1 pr-1"
      data-testid="send-provenance"
      data-via={provenance.via}
      data-mode={provenance.mode ?? ''}
    >
      {splitOnPlaceholder(sentence, 'sender').map((part, i) =>
        part === null ? <span key={i}>{sender}</span> : <span key={i}>{part}</span>,
      )}
    </div>
  )
})

export default SendProvenanceLabel
