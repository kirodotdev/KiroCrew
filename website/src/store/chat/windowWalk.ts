/** Extending a most-recent slot-detail window OLDER, one bounded page at a time,
 *  until it reaches the rows a tab already holds. `refreshSlot`, `switchSlot` and
 *  `warmSlotCache` take this instead of re-reading the whole chained transcript
 *  when their window misses what the tab holds. */
import { api } from '../../api/client'
import type { ChatMessage } from '../../types'
import { devLog, inspectorOn } from '../../dev/scrollInspector'
import { fetchSlotDetail, normalizeSlotDetail } from './wire'
import { idAnchorsOneRow, midOccurrences } from './transcript'
import { SLOT_DETAIL_MAX_LIMIT } from './paging'

export type SlotDetailPage = Awaited<ReturnType<typeof fetchSlotDetail>>

/** Most older pages `walkWindowBackTo` fetches before it stops reaching for the
 *  rows a tab holds. Each page is `SLOT_DETAIL_MAX_LIMIT` rows, so one refresh
 *  or switch moves at most (2 + this) * 500 rows, however long the transcript --
 *  the walk only ever covers rows the SERVER gained since the tab last read it,
 *  plus the one page that overlaps what the tab already has, plus one re-read of
 *  the newest page so the returned tail is as fresh as the returned status.
 *
 *  Reaching the cap means the gap is wider than the cap covers, or the held rows
 *  carry no `meta.mid` to anchor on (rows written before the backend stamped
 *  ids). The replacing reducer then finds no anchor and keeps no head, so the
 *  rows above the walked window leave the tab -- they are one page-back away,
 *  not gone. That is the cost taken instead of a read whose size is the whole
 *  transcript. */
export const WINDOW_WALK_MAX_PAGES = 8

/** The rows a replacing reducer can anchor a kept head on: `olderHeadAbovePage`
 *  is handed the prior view minus `thinking` (no identity, broadcast-only) and
 *  `permission` (re-seated from the client's own copies), so the walk must judge
 *  reach against exactly that set or it stops on an anchor the reducer will not
 *  see. */
function headAnchorRows(rows: ChatMessage[]): ChatMessage[] {
  return rows.filter(m => m.role !== 'thinking' && m.role !== 'permission')
}

/** Extend a most-recent window OLDER, one bounded page at a time, until its
 *  oldest row anchors one row of `held` -- the condition under which the
 *  replacing reducers (`switchSlot`, `refreshSlot`) keep the rows above it
 *  (`olderHeadAbovePage`) -- or the transcript's start is reached, or the page
 *  cap is spent.
 *
 *  This is what an unbounded re-read used to buy: a page that provably covers
 *  the view, so replacing the view with it deletes no scrollback. The difference
 *  is the price -- the walk pays for the rows the server GAINED, the unbounded
 *  read paid for the whole transcript every time, and on a long session that
 *  ran on every `chat_done` (measured: 11,134 rows re-read per turn).
 *
 *  Same predicate as the reducers, not a looser one. A window is safe to replace
 *  the held rows with on either of two counts: its oldest row anchors one held
 *  row (`olderHeadAbovePage` then keeps the rows above it), or the oldest HELD
 *  row anchors one row of the window (the window is a superset, and replacing
 *  with a superset loses nothing). Both go through `idAnchorsOneRow` over the
 *  accumulated window, so a `mid` seen twice (two slices never overlap, so only
 *  a caller-repeated id does this) declines exactly as the reducer's cut would.
 *
 *  Returns the input page untouched when there is nothing to reach (`held`
 *  empty -- a cold view has no scrollback to protect, and a wider window would
 *  be one nothing asked for), when the page already reaches the start, or when
 *  it already anchors. */
export async function walkWindowBackTo(key: string, page: SlotDetailPage, held: ChatMessage[]): Promise<SlotDetailPage> {
  const anchor = headAnchorRows(held)
  const anchorCounts = midOccurrences(anchor)
  const reaches = (rows: ChatMessage[]): boolean => {
    const rowCounts = midOccurrences(rows)
    return idAnchorsOneRow(rows[0]?.meta?.mid, anchor, rows, anchorCounts, rowCounts)
      || idAnchorsOneRow(anchor[0]?.meta?.mid, anchor, rows, anchorCounts, rowCounts)
  }
  if (anchor.length === 0 || !page.hasMore || reaches(page.messages)) return page
  let messages = page.messages
  let before = page.nextBefore
  let hasMore = page.hasMore
  let pages = 0
  // The newest status snapshot the walk saw. Every page is a full handler
  // response, so the LAST one answered latest: a turn that ends while the walk
  // is in flight shows up here as `running: false`, and the first page's
  // `running: true` must not outlive it (it would re-arm the run state over the
  // `_done` that already landed). Rows still come from every page.
  let latest: SlotDetailPage = page
  // Two independent stops besides the anchor: the page cap (the whole point of
  // walking instead of reading unbounded), and a cursor that failed to move
  // older -- a server answering the same `next_before` twice would otherwise
  // keep this loop alive for as long as it keeps saying `has_more`.
  while (hasMore && before > 0 && pages < WINDOW_WALK_MAX_PAGES) {
    const older = normalizeSlotDetail(key, await api.chatSlotDetail(key, SLOT_DETAIL_MAX_LIMIT, before), true)
    pages += 1
    latest = older
    messages = [...older.messages, ...messages]
    const nextBefore = older.nextBefore
    hasMore = older.hasMore
    const stalled = nextBefore >= before
    before = nextBefore
    if (stalled || !hasMore || reaches(messages)) break
  }
  if (inspectorOn()) {
    devLog('WALK', `${key} pages=${pages} rows=${messages.length} reached=${!hasMore || reaches(messages)} held=${anchor.length}`)
  }
  /* The newest edge must be as fresh as the status. Every row the walk added is
   * OLDER than the first page, so after `pages` more round trips the first page's
   * tail can be stale -- a turn that finished meanwhile left a partial reply
   * there, and pairing it with the latest `running: false` would publish that
   * truncated reply as final with nothing to correct it while the socket is down.
   * Re-read the newest page once (one bounded request, only on the walk path) and
   * splice it in at the first row it shares with the window. When it shares
   * none -- more rows landed than a page holds -- keep the FIRST page's status
   * instead, so rows and status at least describe the same moment and the next
   * refresh reconciles both. */
  let status: SlotDetailPage = latest
  let newestEdge: { total: number | undefined } = page
  if (pages > 0) {
    const newest = normalizeSlotDetail(key, await api.chatSlotDetail(key, SLOT_DETAIL_MAX_LIMIT), true)
    const windowCounts = midOccurrences(messages)
    const newestCounts = midOccurrences(newest.messages)
    const seamMid = newest.messages[0]?.meta?.mid
    const seam = idAnchorsOneRow(seamMid, messages, newest.messages, windowCounts, newestCounts)
      ? messages.findIndex(m => m.meta?.mid === seamMid)
      : -1
    if (seam >= 0) {
      messages = [...messages.slice(0, seam), ...newest.messages]
      status = newest
      newestEdge = newest
    } else if (!newest.hasMore) {
      // The newest read already starts at the transcript's beginning: it IS the
      // whole window, freshly.
      messages = newest.messages
      hasMore = false
      before = newest.nextBefore
      status = newest
      newestEdge = newest
    } else {
      status = page
    }
  }
  // `total` describes the transcript whose newest edge the returned rows carry,
  // and it is the count the baseline is compared in.
  return {
    ...page,
    total: newestEdge.total,
    running: status.running,
    stopping: status.stopping,
    queue: status.queue,
    context: status.context ?? page.context,
    messages,
    nextBefore: before,
    hasMore,
  }
}
