/**
 * One message that a crewmate sent to another crewmate (`message_mate`).
 *
 * The gateway records the message as a `card` row in BOTH threads
 * (`dashboard/chat_cards.record_peer_message`): `out` in the sender's thread,
 * `in` in the target's. The row is display-only. The target model gets the
 * text only in the wake that wraps it as data from the sender, so this row is
 * how the user sees what crossed between the two crewmates.
 *
 * The text is drawn as plain text, never as Markdown or HTML, because another
 * agent wrote it.
 */
import { ArrowDownLeft, ArrowUpRight } from 'lucide-react'
import { i18nT } from '../i18n/t'

export interface PeerMessageRef {
  surface: 'peer'
  id: string
  slot: string
  direction: 'in' | 'out'
  /** The other crewmate's crew name. */
  peer: string
  /** The message as the gateway recorded it: redacted and bounded. */
  text: string
  hop: number
}

export default function PeerMessageRow({ message }: { message: PeerMessageRef }) {
  const inbound = message.direction === 'in'
  const label = i18nT(inbound ? 'components.peerMessage.from' : 'components.peerMessage.to', { name: message.peer })
  const Icon = inbound ? ArrowDownLeft : ArrowUpRight
  return (
    <div
      className="rounded-lg border border-border bg-bg-elevated text-text px-3 py-2 text-[13px]"
      data-testid="peer-message-row"
      data-peer-direction={message.direction}
    >
      <div className="flex items-center gap-2 text-[12px] text-muted">
        <Icon size={12} className="lucide-inline shrink-0" aria-hidden="true" />
        <span className="min-w-0 truncate" data-testid="peer-message-label">{label}</span>
      </div>
      <p className="mt-1 whitespace-pre-wrap break-words" data-testid="peer-message-text">{message.text}</p>
    </div>
  )
}
