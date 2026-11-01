import { useAppSelector } from '../store'
import ConcurrencyCutChip, { SessionQueuedMarker } from './ConcurrencyCutChip'
import InboundLinkChip from './InboundLinkChip'
import { slotRowByKey } from '../store/slotIndex'

/**
 * The status chips the chat header shows after the session title, in order:
 * the inbound-channel link, the adaptive controller's concurrency cut, and the
 * open session's own queued marker. Each renders nothing (or an empty live
 * region) when it has nothing to say, so the row only grows while one does.
 *
 * While the inbound chip shows, the row is `crowded`: the cut chip and the
 * marker step down to their narrow forms at a wider row, so the three chips
 * and the title still share it.
 */
export default function ChatHeaderChips({ slotKey }: { slotKey?: string }) {
  // The same test InboundLinkChip renders on: a two-way link on this slot.
  const crowded = useAppSelector(s => !!slotKey
    && !!slotRowByKey(s.dashboard.slots, slotKey)?.links?.some(link => link.direction === 'both'))
  return (
    <>
      <InboundLinkChip slotKey={slotKey} />
      <ConcurrencyCutChip crowded={crowded} />
      <SessionQueuedMarker slotKey={slotKey} crowded={crowded} />
    </>
  )
}
