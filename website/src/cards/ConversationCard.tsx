/**
 * A change card or guide offer, drawn in the conversation at the point
 * it was proposed.
 *
 * The gateway records each proposal as a `card` row of the slot's transcript
 * (`dashboard/chat_cards.py`), so the row's position IS where the agent proposed
 * it, live and after a reload. The row carries only a reference -- surface, id,
 * kind, title and the last status the gateway recorded -- and nothing that
 * could authorize anything: the live card comes from the card store's own read
 * (`useChangeCards`, `card_update` frames) and the live offer from the guide
 * store's (GuideContext), so the card updates in place and every apply still
 * goes through the card's own plan and the gateway's hook.
 *
 * When the store no longer holds the card (finished more than a day ago, or
 * dismissed), the row's recorded status is drawn as a one-line result with no
 * actions. The transcript is agent-writable, so a row is only ever matched to
 * a card of THIS slot; a row naming another slot draws its record line only.
 */
import type { ChatMessage } from '../types'
import { useAppSelector } from '../store'
import { i18nT } from '../i18n/t'
import { CheckCircle2, AlertTriangle, RotateCcw, X, Clock } from 'lucide-react'
import { CARD_FINISHED_STATUSES, type CardStatus } from '../api/cards'
import { useChangeCards } from './useChangeCards'
import ChangeCard from './ChangeCard'
import { useCardCorners } from './cardCorners'
import GuideOfferCard from '../guide/GuideOfferCard'
import ErrorNotice from '../components/ErrorNotice'

export const CARD_ROLE = 'card'

export interface CardRef {
  surface: 'change' | 'guide'
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
  const surface = c.surface === 'change' || c.surface === 'guide' ? c.surface : null
  const id = str(c.id, 64)
  if (!surface || !id) return null
  const reason = str(c.reason, 32)
  return { surface, id, slot: str(c.slot, 256), kind: str(c.kind, 64), title: str(c.title), status: str(c.status, 32), ...(reason ? { reason } : {}) }
}

/** A finished (or forgotten) change card, from the row's own record. */
export function CardRecordLine({ card }: { card: CardRef }) {
  const corners = useCardCorners()
  const finished = CARD_FINISHED_STATUSES.has(card.status as CardStatus)
  // A recorded proposal the store no longer holds never finished: it lapsed.
  const status = finished ? card.status : 'expired'
  const title = card.title || card.kind
  const text = status === 'applied' ? i18nT('components.changeCards.result_applied', { title })
    : status === 'partial' ? i18nT('components.changeCards.result_partial', { title })
    : status === 'undone' ? i18nT('components.changeCards.result_undone', { title })
    : status === 'cancelled' ? i18nT('components.changeCards.result_cancelled', { title })
    : status === 'failed' ? i18nT('components.changeCards.result_failed', { title })
    : i18nT('components.changeCards.result_expired', { title })
  const Icon = status === 'applied' ? CheckCircle2 : status === 'partial' || status === 'failed' ? AlertTriangle
    : status === 'undone' ? RotateCcw : status === 'cancelled' ? X : Clock
  const tone = status === 'applied' ? 'text-ok' : status === 'partial' || status === 'failed' ? 'text-warn' : 'text-muted'
  const muted = status !== 'applied' && status !== 'partial'
  return (
    <div
      data-testid="change-card-record"
      data-card-status={status}
      className={`${corners} border border-border bg-card text-card-fg px-4 py-1.5`}
    >
      <div className="flex min-h-11 items-center gap-2">
        <Icon size={16} className={`shrink-0 ${tone}`} aria-hidden="true" />
        <span className={`min-w-0 flex-1 break-words text-[13px] ${muted ? 'text-muted' : 'text-text'}`}>{text}</span>
      </div>
    </div>
  )
}

function ChangeCardRow({ card, slot }: { card: CardRef; slot: string | null }) {
  const own = !!slot && card.slot === slot
  const { cards, error, isFetched, store, refresh, remove } = useChangeCards(own ? slot : null)
  const live = own ? cards.find(c => c.id === card.id && c.slot_key === slot) : undefined
  if (live) return <ChangeCard card={live} store={store} refresh={() => void refresh()} remove={remove} />
  const finished = CARD_FINISHED_STATUSES.has(card.status as CardStatus)
  if (own && error) {
    // The store could not be read, so its silence says nothing: an unfinished
    // record is NOT drawn as expired, and the failure itself is shown.
    const message = error instanceof Error && error.message ? error.message : String(error)
    return (
      <div className="flex flex-col gap-2">
        {finished && <CardRecordLine card={card} />}
        {/* No hand-off: the row sits in the chat beside the composer, whose unsent draft the hand-off navigation would discard. */}
        <ErrorNotice variant="inline" testId="change-card-read-error" message={message} />
      </div>
    )
  }
  // Until the store answers, an unfinished record could still be a live card.
  if (own && !isFetched && !finished) return null
  return <CardRecordLine card={card} />
}

export default function ConversationCard({ message, slot }: { message: ChatMessage; slot?: string | null }) {
  const activeSlot = useAppSelector(s => s.chat.activeSlot)
  const card = readCardRef(message)
  if (!card) return null
  const here = slot ?? activeSlot ?? null
  return (
    <div className="min-w-0" data-testid="conversation-card" data-card-surface={card.surface} data-card-id={card.id}>
      {card.surface === 'change'
        ? <ChangeCardRow card={card} slot={here} />
        : <GuideOfferCard guideId={card.id} slotKey={here} recordedStatus={card.status} recordedReason={card.reason ?? null} />}
    </div>
  )
}
