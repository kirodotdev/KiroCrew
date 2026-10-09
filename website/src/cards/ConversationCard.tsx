/**
 * A guide offer, drawn in the conversation at the point it was offered.
 *
 * The gateway records each offer as a `card` row of the slot's transcript
 * (`dashboard/chat_cards.py`), so the row's position IS where the agent offered
 * it, live and after a reload. The row carries only a reference -- surface, id,
 * kind and the last status the gateway recorded -- and nothing that could
 * authorize anything: the live offer comes from the guide store's own read
 * (GuideContext), so it updates in place.
 *
 * The transcript is agent-writable, so a row is only ever matched to a guide of
 * THIS slot.
 */
import type { ChatMessage } from '../types'
import { useAppSelector } from '../store'
import GuideOfferCard from '../guide/GuideOfferCard'

export const CARD_ROLE = 'card'

export interface CardRef {
  surface: 'guide'
  id: string
  slot: string
  kind: string
  title: string
  status: string
  /** A guide's recorded end reason, when it changes the result line. */
  reason?: string
}

const str = (v: unknown, max = 300) => (typeof v === 'string' ? v.slice(0, max) : '')

/** The row's card reference, or null when the row does not carry a usable one. */
export function readCardRef(m: Pick<ChatMessage, 'role' | 'meta'>): CardRef | null {
  if (m.role !== CARD_ROLE) return null
  const raw = (m.meta as { card?: unknown } | undefined)?.card
  if (!raw || typeof raw !== 'object') return null
  const c = raw as Record<string, unknown>
  const id = str(c.id, 64)
  if (c.surface !== 'guide' || !id) return null
  const reason = str(c.reason, 32)
  return { surface: 'guide', id, slot: str(c.slot, 256), kind: str(c.kind, 64), title: str(c.title), status: str(c.status, 32), ...(reason ? { reason } : {}) }
}

export default function ConversationCard({ message, slot }: { message: ChatMessage; slot?: string | null }) {
  const activeSlot = useAppSelector(s => s.chat.activeSlot)
  const card = readCardRef(message)
  if (!card) return null
  const here = slot ?? activeSlot ?? null
  return (
    <div className="min-w-0" data-testid="conversation-card" data-card-surface={card.surface} data-card-id={card.id}>
      <GuideOfferCard guideId={card.id} slotKey={here} recordedStatus={card.status} recordedReason={card.reason ?? null} />
    </div>
  )
}
