/** How much history a read asks for and whether what came back covers what
 *  the tab already holds: page sizes, the switch and count-matched limits, the
 *  coverage shortfall, the paging-cursor shift after a kept head, and the abort
 *  handle of the one older-history page in flight. */
import type { ChatMessage } from '../../types'
import { hasUnidentifiedDurableRow, isDurableRow, transcriptTsMs } from './transcript'

/** Rows for the initial slot-open page and each older-history page. One size
 *  for both keeps the scrollback walk uniform: the first page a slot opens
 *  with is simply page one of the same pagination `loadOlderMessages` runs. */
export const OLDER_PAGE_LIMIT = 100

/** Page size for walking BACK through history (loadOlderMessages).
 *
 * Equal to OLDER_PAGE_LIMIT: a load is a load, and the reader cannot tell which
 * door issued it. The larger page this used to carry was justified by amortizing
 * round trips across a walk that ran to the START of history ("a 13-page walk
 * becomes 5") -- but the walk is now bounded to a few pages per expression of
 * intent, so it cannot reach the start on one gesture no matter how big its page
 * is, and the amortization has nothing left to amortize. What the big page did
 * instead was multiply the cost of a single flick: measured on a phone, one
 * gesture pulled 706 rows / ~260,000px of transcript with the reader's finger
 * nowhere near the screen, which reads as history loading without end.
 *
 * The unit is worth stating because it is the trap: this counts MESSAGES, while
 * a reader consumes SCREENS. Measured on the reporter's device a display row is
 * 0.6-1.2 viewports, so ~3 messages fill a screen -- one page of 100 is already
 * some 30 screens of reading. A page that looks modest in messages is enormous
 * in the unit the reader actually experiences. */
export const OLDER_WALK_PAGE_LIMIT = OLDER_PAGE_LIMIT

/** The handler's own ceiling (`min(int(limit), 500)` in chat_handlers). Asking
 *  for more is silently clamped, so a caller that needs to KNOW whether its
 *  window covered the cache has to compare against this, not against what it
 *  asked for. */
export const SLOT_DETAIL_MAX_LIMIT = 500

/**
 * Rows to request when switching to a slot, or `undefined` for the unbounded
 * shape.
 *
 * A switch used to go UNBOUNDED for any slot with rows already painted, and the
 * comment beside it carried its own measurement: 6.2MB/~1s unbounded against
 * 0.7MB/57ms bounded. So the FIRST visit to a session was the fast one and every
 * return to it paid for the whole chained transcript — on a 43MB session that is
 * the reported "switching chats got slow and janky", and it got worse as more
 * history became reachable.
 *
 * The reason for going unbounded was real but narrower than the rule: a bounded
 * page is a WINDOW, and if the server grew past it the window could sit entirely
 * newer than the cache, leaving a hole in the middle of the transcript. That is
 * a question of COVERAGE, not of boundedness — and coverage is VERIFIED after the
 * response (`slotCoverageShortfall` asks which cached rows the window does not
 * contain), so it does not have to be pre-purchased with a larger window.
 *
 * Which matters because the window extends BACKWARD from the newest row: every
 * row of headroom is a row of OLDER history nobody asked for. Buying a page of
 * margin therefore grew the transcript upward by a page on every revisit, and
 * since the next revisit measures the cache it just grew, it ratcheted — one page
 * per switch until the handler ceiling. Reported from a phone as history loading
 * itself on every session switch, from a reader parked at the live end, with no
 * gesture and no spinner (this path never sets `loadingOlder`, so it is invisible
 * to every guard on the automatic older-history doors).
 *
 * So ask for exactly what this tab already holds — never fewer than one page —
 * and let the coverage check pay for the rare case instead.
 *
 * A STREAMING slot is not an exception to that, though it used to be. The
 * carve-out rested on the same pre-purchase argument the paragraph above
 * retires: unseen growth can push a window clear of a small cache. That is the
 * hole the coverage check verifies for, and it verifies it for a streaming
 * response exactly as it does for a settled one — so the streaming exemption was
 * the retired argument surviving in the one branch that did not get revisited.
 *
 * What it cost is the whole point of bounding: a slot mid-turn is the most likely
 * slot a reader switches away from and back to, so the exemption applied the
 * unbounded shape to the commonest switch there is. Measured on a phone as one
 * switch into a streaming session turning 303 loaded messages into 6,265 (7,303
 * raw rows, ~293,000px of transcript) with no gesture, no spinner, and no paging
 * door involved -- and, because the whole transcript is replaced at once, the
 * reader's saved position with it.
 *
 * Order matters here: bounding this is only safe once a streaming response can
 * leave a comparable baseline behind (`retainServerTotal`). Without one the
 * coverage check cannot prove overlap, and every streaming switch would take the
 * unbounded RETRY instead — the same payload, one round-trip later.
 */
export function slotSwitchFetchLimit(input: {
  cached: number
  pageLimit?: number
  maxLimit?: number
}): number | undefined {
  const pageLimit = input.pageLimit ?? OLDER_PAGE_LIMIT
  const maxLimit = input.maxLimit ?? SLOT_DETAIL_MAX_LIMIT
  if (input.cached <= 0) return pageLimit
  return Math.min(maxLimit, Math.max(pageLimit, input.cached))
}

/** The fields coverage needs off a transcript row. Structural rather than the full
 *  `ChatMessage`, so the contract is readable and testable without a whole message. */
export type CoverageRow = {
  ts?: string
  role?: string
  content?: unknown
  meta?: { mid?: unknown }
}

/** A row's identity for the coverage test, in the same vocabulary `deduplicateByMid`
 *  uses:
 *  the server-minted `meta.mid` with `role` and the instant, since a mid can be
 *  supplied by a caller and is not trustworthy alone. A row with no mid falls back to
 *  its content, which is what the transcript itself renders and the only thing left to
 *  compare.
 *
 *  A mid is matched ALONE, without role or timestamp beside it. Every other field on a
 *  row is mutable while the mid is not: a `ts` is overwritten from the optimistic client
 *  value to the server's authoritative one (`sseChatMessage` stashes the old one as
 *  `meta.clientTs` precisely because it changes), a role flips `streaming` -> `assistant`
 *  on finalization, and content grows from partial to final. Pairing any of them with a
 *  stable id defeats the id: the same row read twice reports as two rows, and the
 *  resulting false shortfall reloads the whole transcript -- after nothing more exotic
 *  than sending a message and switching slots. `deduplicateByMid` does pair mid with role
 *  and ts, for a different job: it COLLAPSES rows in the rendered transcript, so it must
 *  not let a crafted mid hide a legitimate row. Coverage cannot be fooled that way
 *  because it COUNTS -- two cached rows carrying one mid still need two window rows
 *  carrying it -- so the discrimination that dedup needs costs coverage nothing to drop.
 *
 *  Without a mid the fallback keys on the INSTANT rather than the raw `ts`: the
 *  seconds-or-ISO union means one row can be spelled two ways, and an identity that
 *  changed with the spelling would call the same row two rows.
 *
 *  Two rows can still be genuinely indistinguishable -- the same role, instant and text,
 *  with no mid on either. Coverage counts them rather than deduplicating them, so a
 *  cache holding two and a window holding one reports the one that would be lost.
 *
 *  A JSON array rather than a delimiter-joined string: no delimiter can collide with
 *  field content, and no string literal here trips the zero-tolerance i18n gate. */
function coverageRowIdentity(r: CoverageRow): string {
  const mid = r.meta?.mid
  if (typeof mid === 'string' && mid) return JSON.stringify([mid])
  return JSON.stringify([null, r.role ?? null, transcriptTsMs(r.ts), String(r.content ?? '')])
}

/**
 * How many rows the tab already holds would be LOST if the bounded window replaced
 * them -- the multiset of cached rows the window does not contain.
 *
 * This is the definition, not a proxy for it. The totals cannot answer the question:
 * a tab holding one page of a long transcript and a tab whose slot grew past its cache
 * look identical as counts (`cached` small, `serverTotal` large), so a count comparison
 * has to assume the worst whenever it has no earlier total to subtract -- which is every
 * FIRST visit to a slot. That assumption read an entire transcript to close a gap that
 * was not there: measured on a phone as 110 loaded messages becoming 2,645 with a server
 * total of 2,644, on a slot whose window already covered its cache exactly.
 *
 * Ordering cannot answer it either, and three rounds of boundary defects came from
 * trying: comparing timestamps as strings, then treating a row tied with the window's
 * oldest as covered, then collapsing two identical rows into one. Each is a different
 * way for "is this row in that set" to be inferred from "is this row older than that
 * row" -- so the fix is to ASK the real question. A row is covered when the window
 * holds a matching row, and each window row can cover only ONE cached row, which is
 * what makes duplicates count.
 *
 * A row the server does not KEEP is skipped on both sides, read through the shared
 * `isDurableRow`. `CLIENT_ONLY_ROLES` -- `queued`, `streaming`, `thinking`,
 * `permission` -- exist only in this client, so the server's window cannot contain one
 * however wide it is asked to be. Counting one as missing is therefore not a hole that
 * a bigger read closes: it is a shortfall that never goes away, so EVERY switch into a
 * slot holding a queued message or a permission card refetches the whole transcript.
 * A narrower test came first here -- skip a row whose `ts` cannot be read -- which
 * happened to catch `streaming` and missed the other three, and the same file already
 * carried the right predicate two consumers deep.
 *
 * The unreadable-`ts` skip stays, for its own reason rather than that one: a row that
 * cannot be placed in time has no whole identity key to match on. An unstamped row is
 * also a live TAIL row, which a newest-N window necessarily reaches, so skipping it
 * cannot hide a hole above it.
 *
 * The one case that declines outright is a window with NO comparable row at all:
 * nothing to compare against, and such a window replacing a populated cache is the
 * shrink this guard is for.
 */
export function slotCoverageShortfall(input: {
  cached: readonly CoverageRow[]
  window: readonly CoverageRow[]
}): number {
  const { cached, window: win } = input
  // Rows this comparison can say anything about at all. Two independent reasons a row
  // is excluded, and they are NOT the same question:
  //   `isDurableRow` -- can the server's window contain this row even in principle?
  //   a readable `ts`  -- can the row be placed, so its identity key is whole?
  const comparable = (r: CoverageRow) => isDurableRow(r) && transcriptTsMs(r.ts) !== null
  const held = cached.filter(comparable)
  if (held.length === 0) return 0
  const floor = win.filter(comparable)
  // Nothing to compare against: an unplaceable window replacing a populated cache is
  // the shrink this guard is for. Counted over the COMPARABLE cache, not the whole of
  // it, or a slot holding only client-only rows against an empty window reports a
  // shortfall it cannot lose.
  if (floor.length === 0) return held.length
  // The window as a MULTISET: a row present twice can cover two cached rows, and a row
  // present once can only cover one.
  const have = new Map<string, number>()
  for (const r of floor) {
    const k = coverageRowIdentity(r)
    have.set(k, (have.get(k) ?? 0) + 1)
  }
  let outside = 0
  for (const r of held) {
    const k = coverageRowIdentity(r)
    const n = have.get(k) ?? 0
    if (n > 0) have.set(k, n - 1)
    else outside += 1
  }
  return outside
}

// Aborts the in-flight older-history fetch, or null when none is running.
// Module-level because switchSlot must reach a fetch it did not start.
let activeOlderAbort: (() => void) | null = null

/** Record the abort handle of the older-history page now in flight. */
export function claimOlderFetchAbort(abort: () => void): void {
  activeOlderAbort = abort
}

/** Drop the handle, but only our own: a newer fetch may already have replaced it. */
export function releaseOlderFetchAbort(abort: () => void): void {
  if (activeOlderAbort === abort) activeOlderAbort = null
}

/** Abort any in-flight older-page fetch. Wired to transcript MOTION: the
 *  settle gates guard the DISPATCH moment, but a page dispatched during a
 *  reading pause lands 1-2s later — mid-fling on a phone, where the prepend
 *  compensation fights the momentum curve (reproduced on the momentum rig as
 *  ±3000px content jumps during coast). Aborting on motion means a landing
 *  can only ever commit while the scroller is still; the walk re-dispatches
 *  when stillness returns. An abort rejection carries no payload, so the
 *  rejected reducer sets no error flag. */
export function abortActiveOlderFetch(): void {
  activeOlderAbort?.()
}

/**
 * True for a rejection that means "this paging attempt was cancelled or refused",
 * as opposed to "this request failed". A caller must not read either as evidence
 * that the history it wanted is unreachable.
 *
 * `AbortError` is a superseded fetch (the user switched chat). `ConditionError`
 * is Redux Toolkit refusing the dispatch outright — a page is already loading,
 * or the cursor belongs to a chat the user has left.
 *
 * Keys on `name` and deliberately does NOT use `instanceof`: `unwrap()`
 * rethrows Redux Toolkit's serialized error, a plain `{name, message, stack}`
 * object. The `instanceof DOMException` / `instanceof Error` form used
 * elsewhere in this codebase is always false here, so it would silently never
 * match.
 */
export function isSupersededPagingRejection(err: unknown): boolean {
  if (!err || typeof err !== 'object') return false
  const name = (err as { name?: unknown }).name
  return name === 'AbortError' || name === 'ConditionError'
}

/** Messages a background pane hydrates. Bounds both pane hydrate paths: the
 *  pane's own query and `warmSlotCache`, so `has_more` matches what it holds. */
export const PANE_HYDRATE_LIMIT = 50

/** The count-matched limit for a refetch that REPLACES rows it did not page, or
 *  `undefined` when no bound can be proven safe for the rows the caller holds.
 *
 *  ONE owner for two paths that ask the identical question: `refreshSlot` about the
 *  open transcript, `warmSlotCache` about a background pane's cache. Both hand the
 *  response to a reducer that reconciles it by `meta.mid`, so a bound is safe for
 *  both under exactly one condition and unsafe for both under exactly one other --
 *  and a rule proved for one must not be able to go missing from the other.
 *
 *  `rows` is what the caller already holds. Only DURABLE rows are counted: the limit
 *  reaches a handler that slices DISK, disk holds no client-only row (a `thinking`
 *  block, a `permission` card, a `queued` bubble), and counting one inflates the
 *  request past the caller's own span. `floor` keeps a near-empty view from asking
 *  for a single row; `ceiling` is the widest window worth a round trip.
 *
 *  `span` picks WHICH durable rows are counted, and it must match the check the
 *  caller runs on the page it gets back:
 *
 *  - `identified` (default, `refreshSlot`): rows carrying a `mid`. Its post-fetch
 *    checks are `mid`-keyed, and the floor guard below is argued over that count.
 *  - `placeable` (`warmSlotCache`): rows carrying a `mid` OR a readable `ts` -- the
 *    exact rows `slotCoverageShortfall` measures the page against. Counting only
 *    `mid` rows there sized the page SMALLER than the span coverage checks: 50+
 *    identified rows plus one mid-less legacy row asked for one row too few, so the
 *    shortfall was always > 0 and every warm paid the bounded read AND the unbounded
 *    retry this limit exists to cap. The floor guard still holds: at `want === held`
 *    a page of `held` rows is the whole placeable span, and above it the decline for
 *    unidentified history is unchanged.
 *
 *  Declines in the two shapes where a window can strand a row the caller holds:
 *
 *  - NOTHING IDENTIFIED. With no `mid` to count there is no span to match, so any
 *    number would be a FIXED bound -- a window that can sit entirely newer than the
 *    cache, which the reducer must then replace rather than merge.
 *  - THE FLOOR OVER-REQUESTS INTO UNIDENTIFIED HISTORY. At `want === held` a page of
 *    `held` rows leaves no room for an unidentified row to be its oldest, so the cut
 *    anchors. Above `held` the floor pulls older unidentified rows in, the page's
 *    oldest anchors nothing, and the reducer keeps no head while the caller still
 *    holds rows above it -- in no page and no head. Legacy history written before the
 *    backend stamped `mid` is the real case. */
export function countMatchedFetchLimit(input: {
  rows: readonly ChatMessage[]
  floor: number
  ceiling: number
  span?: 'identified' | 'placeable'
}): number | undefined {
  const { rows, floor, ceiling, span = 'identified' } = input
  const hasMid = (m: ChatMessage) => typeof m.meta?.mid === 'string' && m.meta.mid.length > 0
  // Nothing identified: no span to match, so any number would be a fixed bound.
  if (!rows.some(m => isDurableRow(m) && hasMid(m))) return undefined
  const counted = span === 'placeable'
    ? (m: ChatMessage) => hasMid(m) || transcriptTsMs(m.ts) !== null
    : hasMid
  const held = rows.filter(m => isDurableRow(m) && counted(m)).length
  const want = Math.max(held, floor)
  if (want > ceiling) return undefined
  if (want > held && hasUnidentifiedDurableRow(rows as ChatMessage[])) return undefined
  return want
}

/** The `(hasMore, cursor)` pair to install after keeping an older head above a
 *  bounded page. Lives here so a second head-keeping reducer cannot re-derive it.
 *  The cursor is a row OFFSET, so a kept head shifts it down by the head's own
 *  server-row count -- and a `Math.max(0, ...)` clamp conflates two OPPOSITE ends:
 *  - EXACT (`headRows === nextBefore`): the head covers `[0, nextBefore)`, so
 *    everything older is held and `hasMore` must go FALSE. True at cursor 0
 *    advertises history behind an offset `loadOlderMessages` refuses
 *    (`slotOldestIndex <= 0`) -- a PERMANENT dead click, not the one-shot one an
 *    unshifted cursor costs.
 *  - DISAGREEMENT (`headRows > nextBefore`): completeness is NOT proven, so
 *    flipping `hasMore` false would STRAND real history. Fall back to the page's
 *    own cursor -- the one-shot dead click, which self-heals on the next page.
 */
export function pagingCursorAfterKeptHead(
  hasMore: boolean,
  nextBefore: number,
  headRows: number,
): { hasMore: boolean; nextBefore: number } {
  if (headRows <= 0) return { hasMore, nextBefore }
  // Completeness proven: nothing older remains to fetch.
  if (headRows === nextBefore) return { hasMore: false, nextBefore: 0 }
  // Counts disagree, so decline to claim completeness rather than strand rows.
  if (headRows > nextBefore) return { hasMore, nextBefore }
  return { hasMore, nextBefore: nextBefore - headRows }
}

/** Upper bound on the count-matched `refreshSlot` limit, matching the ceiling
 *  the slot-detail handler clamps `limit` to. A request above it comes back
 *  SHORT of what was asked for, which for an in-place replacement would mean the
 *  view SHRINKS -- so a view paged back past this asks for exactly this many
 *  and lets `walkWindowBackTo` reach the rest one page at a time. */
export const REFRESH_LIMIT_CEILING = SLOT_DETAIL_MAX_LIMIT
