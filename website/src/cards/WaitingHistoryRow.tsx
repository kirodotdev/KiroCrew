/**
 * One line in the chat for a waiting-on-you ask coming or going.
 *
 * The gateway records each pin and each retirement of an ask as a `card` row of
 * the slot's transcript (`dashboard/chat_cards.record_waiting`), so the history
 * keeps where an ask was raised and how it closed after its pinned card is gone,
 * live and after a reload. The live ask is the store's and is drawn above the
 * composer (`components/waitingOnYou`); this row only records, so it has nothing
 * to press. Drawn centred and quiet like `LessonSavedRow`.
 */
import { Pin } from 'lucide-react'
import { i18nT } from '../i18n/t'

export type WaitingHistoryStatus = 'pinned' | 'answered' | 'dismissed' | 'cleared' | 'expired' | 'retired'

export interface WaitingHistoryRef {
  surface: 'waiting'
  id: string
  slot: string
  /** The ask's title as the gateway recorded it: cleaned and bounded. */
  title: string
  status: WaitingHistoryStatus
}

const HISTORY_KEYS: Record<WaitingHistoryStatus, string> = {
  pinned: 'components.waitingOnYou.history_pinned',
  answered: 'components.waitingOnYou.history_answered',
  dismissed: 'components.waitingOnYou.history_dismissed',
  cleared: 'components.waitingOnYou.history_withdrawn',
  expired: 'components.waitingOnYou.history_expired',
  retired: 'components.waitingOnYou.history_closed',
}

export function isWaitingHistoryStatus(value: unknown): value is WaitingHistoryStatus {
  return typeof value === 'string' && Object.prototype.hasOwnProperty.call(HISTORY_KEYS, value)
}

export default function WaitingHistoryRow({ record }: { record: WaitingHistoryRef }) {
  const text = i18nT(HISTORY_KEYS[record.status], { title: record.title })
  return (
    <div className="flex flex-col items-center" data-testid="waiting-history-row" data-waiting-status={record.status}>
      <div className="inline-flex max-w-full items-center gap-2 rounded-2xl border border-border bg-bg-elevated px-3 py-1.5 text-[12px] text-muted">
        <Pin size={12} className="lucide-inline shrink-0" aria-hidden="true" />
        <span className="min-w-0 break-words" data-testid="waiting-history-text">{text}</span>
      </div>
    </div>
  )
}
