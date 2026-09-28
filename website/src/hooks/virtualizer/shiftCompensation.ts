// Anchor compensation for the chat virtualizer: holding the reader still while
// the height ABOVE them changes.
//
// While the reader is scrolled up, rows can be inserted, removed or repriced
// above the row they are reading. Native `overflow-anchor: auto` normally
// absorbs that, but a scroll-path recompute can unmount the node the browser
// anchored to, and WebKit ships no scroll anchoring at all -- so the hook
// carries its own anchor. This module owns that machinery end to end: the
// render-phase CAPTURE of the reader's row before a commit moves it
// (useShiftCapture), and the pre-paint CONSUME that re-reads the row after the
// commit and pays the delta back into scrollTop (useShiftCompensation). The
// same render-phase pass plans which row measurements the commit retires or
// renames (HeightIndex.planHeightRetirement), because it is the pass that sees
// which rows left.

import { useCallback, useLayoutEffect, useRef, useState, type MutableRefObject, type RefObject } from 'react'
import { captureAnchorCandsFrom, captureTopAnchorFrom, rowTopFrom } from './anchorGeometry'
import { heightAnchorStillUsable, shiftCompensationAllowed } from './FollowController'
import { planHeightRetirement, type HeightIndex } from './HeightIndex'
import type { WindowRange } from './WindowCalculator'
import type { FollowState, Pinning } from './followPolicy'
import type { ReadingPositionEntry } from './readingPosition'
import type { WindowOperations } from './windowRange'

type Ref<V> = MutableRefObject<V>
type SetWindowRange = (next: WindowRange | ((prev: WindowRange) => WindowRange)) => void

/** Positional re-identification, shared by TRIGGER 1's and the splice
 *  triggers' no-surviving-key fallbacks: a row whose key did not survive the
 *  commit moved by as much as the NEAREST row (by old index) whose key did.
 *  Returns the displacement resolver — built once per commit, queried per
 *  mounted node. `survivors` is in ascending old index, so the nearest is one
 *  of the two neighbours of the change point; ties go to the row ABOVE the
 *  reader. Survivors before `boundary` (the first old index that changed
 *  hands) are excluded: they did not move, so their zero displacement says
 *  nothing about rows at or below the boundary — a splice landing exactly at
 *  the viewport top would otherwise borrow it and anchor the inserted row
 *  itself. TRIGGER 1 passes 0 (a prepend renames index 0, so nothing sits
 *  before its boundary). Only when no survivor remains does `noSurvivorShift`
 *  stand in (the caller's net count change) — the reader then keeps their
 *  distance from the END, the one thing a full re-identification of a chat
 *  transcript preserves, and for a contiguous splice it IS the displacement. */
function nearestSurvivorShiftFrom<T>(
  prevItems: readonly T[],
  prevGetKey: (it: T, i: number) => string,
  newIndexByKey: ReadonlyMap<string, number>,
  noSurvivorShift: number,
  boundary = 0,
): (idx: number) => number {
  const survivors: Array<[oldIndex: number, shift: number]> = []
  for (let i = boundary; i < prevItems.length; i++) {
    const ni = newIndexByKey.get(prevGetKey(prevItems[i], i))
    if (ni !== undefined) survivors.push([i, ni - i])
  }
  return (idx: number): number => {
    if (!survivors.length) return noSurvivorShift
    let lo = 0
    let hi = survivors.length
    while (lo < hi) {
      const mid = (lo + hi) >> 1
      if (survivors[mid][0] < idx) lo = mid + 1
      else hi = mid
    }
    const above = survivors[lo - 1]
    const below = survivors[lo]
    if (!above) return below[1]
    if (!below) return above[1]
    return below[0] - idx < idx - above[0] ? below[1] : above[1]
  }
}

/** A height-sync capture: visible rows and their screen offsets, plus the
 *  scrollTop they were read at (see heightAnchorStillUsable). */
type HeightAnchor = { cands: { key: string; top: number }[]; at: number; scrollTop: number }

export interface ShiftCapture {
  shiftAnchorRef: Ref<{ key: string; top: number } | null>
  shiftStageRef: Ref<'awaiting-rebase' | 'rebased' | 'ready' | null>
  prependCountRef: Ref<number>
  shiftInsertedRef: Ref<number>
  prependPreScrollTopRef: Ref<number>
  prependNetRef: Ref<number>
  rebaseScheduledRef: Ref<boolean>
  heightAnchorPendingRef: Ref<HeightAnchor | null>
  /** TRIGGER 6's invalidation key (see its doc below). */
  spliceCommit: number
  /** Keys of rows that LEFT the list in this render, drained by the height owner. */
  retiredKeysRef: Ref<string[] | null>
  /** Display-key renames detected this render, drained by the height owner. */
  renamedKeysRef: Ref<[string, string][] | null>
  captureAnchorCands: (el: HTMLElement) => { key: string; top: number }[]
  /** The height owner's beforeNotify: capture the reader's rows for a height commit. */
  captureHeightSyncAnchor: () => void
  /** Drop a pending capture that a restore supersedes. */
  dropShiftCapture: () => void
}

export function useShiftCapture<T>(ctx: {
  items: T[]
  getKey: (item: T, index: number) => string
  sessionId: string
  /** The caller's height scope (width bucket); TRIGGER 7 keys on it changing. */
  heightScopeKey: string | undefined
  itemCount: number
  /** The PROP, not its ref -- see the head-page-out note below. */
  onTopReached: (() => void) | undefined
  windowRange: WindowRange
  windowRangeRef: Ref<WindowRange>
  scrollerRef: RefObject<HTMLDivElement | null>
  elIndexRef: Ref<Map<Element, number>>
  itemsRef: Ref<T[]>
  getKeyRef: Ref<(item: T, index: number) => string>
  getStableIdRef: Ref<((item: T, index: number) => string) | undefined>
  anchorIdOf: (item: T, index: number) => string
  follow: Pick<FollowState, 'stickRef'>
}): ShiftCapture {
  const {
    items, getKey, sessionId, heightScopeKey, itemCount, onTopReached, windowRange, windowRangeRef,
    scrollerRef, elIndexRef, itemsRef, getKeyRef, getStableIdRef, anchorIdOf,
  } = ctx
  const { stickRef } = ctx.follow

  /** Collect up to 3 visible rows as height-anchor candidates, priced through
   *  anchorIdOf against the CURRENT items/elIndex pairing. Only meaningful when
   *  those two are consistent — i.e. not mid-prepend-commit, where itemsRef has
   *  already advanced while elIndex still carries pre-shift indices and every
   *  priced key is off by the inserted count (the capture tear behind the
   *  measured −721px dropped correction). */
  const captureAnchorCands = useCallback(
    (el: HTMLElement): { key: string; top: number }[] =>
      captureAnchorCandsFrom(el, elIndexRef.current.entries(), (i) => itemsRef.current[i], anchorIdOf),
    [anchorIdOf, elIndexRef, itemsRef],
  )

  // ---- Scroll-anchor preservation ----
  //
  // While the user is scrolled up reading history, content can grow ABOVE the
  // row they are reading, which moves that row down while scrollTop stays put —
  // that IS the jump. Native `overflow-anchor: auto` normally holds the viewport
  // steady, but a scroll-path recompute can UNMOUNT the browser's chosen anchor
  // node (rows past WINDOW_UNMOUNT_HYSTERESIS), collapsing anchoring. So the
  // hook carries its own anchor: the topmost visible row's key + screen offset,
  // captured BEFORE the shift, re-read after commit, and the delta paid back
  // into scrollTop. This REDUCES reliance on overflow-anchor (it does not
  // replace it — the CSS is owned by ChatPage and left alone).
  // Anchor captured by syncHeightsNow for a spacer-repricing commit. Kept
  // SEPARATE from shiftAnchorRef: that slot is consumed on a windowRange
  // commit, and window commits land constantly while rows
  // mount — sharing the slot lets an unrelated window commit consume (and
  // clear) the anchor before the height-sync commit it was captured for,
  // leaving the repricing shift uncompensated (observed as a nondeterministic
  // 170-190px lurch after a far jump with scroll anchoring unavailable).
  // A LIST of visible candidates, not one anchor. The consumer runs a
  // debounce-beat (120ms) after capture, and during active scrolling the
  // window can move past the single topmost row in that beat — the row
  // unmounts, rowTopFrom answers null, and the correction was dropped
  // wholesale (hLostRow). Measured under fixed-velocity scrolling: a reprice
  // of rows fully above the viewport slid content −721px in one frame with
  // hLostRow ticking and no compensation write. The first candidate still
  // mounted at consume time carries the correction instead; each candidate is
  // an equally valid witness of "how far did the content under the reader
  // move", so falling to the next loses nothing.
  const heightAnchorPendingRef = useRef<{ cands: { key: string; top: number }[]; at: number; scrollTop: number } | null>(null)

  /**
   * ONE compensation routine, SEVEN triggers, TWO slots (see TRIGGER 7).
   *
   *   TRIGGER 1 — prepend (load-older history): every index shifts up, so
   *     pre-existing rows move down by the inserted height.
   *   TRIGGER 2 — window shift (scroll recompute / top-sentinel expansion):
   *     rows mount above the viewport and are re-measured from the flat
   *     estimate, or unmount above it and are replaced by a spacer the tree
   *     prices from stale heights, so the content above the reader changes
   *     height either way.
   *   TRIGGER 3 — tail append (a new message arrives while the reader is
   *     scrolled up): nothing is inserted above them, but the growth re-syncs
   *     the offset tree, and every row that has never been measured is
   *     re-priced from the running MEAN of the measured ones — so the height
   *     credited above the reader changes anyway and the transcript slides
   *     (measured in the harness: a row at screen offset 0 landed at 500).
   *   TRIGGER 4 — mid-list INSERT (a transient "thinking" row mounts between rows
   *     that are already on screen): the count grows and index 0 keeps its key,
   *     which reads exactly like TRIGGER 3, but every index from the splice point
   *     on MOVES. Left on trigger 3's path it anchored on a MIS-KEYED row,
   *     because that path resolves a mounted node's previous-commit index through
   *     the NEW items.
   *   TRIGGER 5 — mid-list REMOVE (that same row unmounts): the height above the
   *     reader SHRINKS and the transcript is pulled up under them. No trigger
   *     covered a shrink at all (issue #6076).
   *   TRIGGER 6 — mid-list SWAP: the thinking row leaves and its replacement
   *     arrives in ONE commit, which React batching makes the ordinary streaming
   *     shape. The net count is unchanged, so a count-delta trigger reads it as a
   *     no-op — and the single consumer is invalidated by `windowRange` and
   *     `itemCount`, neither of which an equal-count commit moves. So this
   *     trigger carries its OWN invalidation key (`spliceCommit`, bumped in the
   *     render that captures here), which is what makes the capture safe: the
   *     anchor is spent in the very commit it describes instead of sitting in the
   *     slot for an unrelated later commit — the stranded-anchor hazard the
   *     render-phase capture exists to remove. It participates in the height
   *     RETIREMENT below on the same commit.
   *   TRIGGER 7 — WIDTH-SCOPE swap (the settled width bucket changes): the
   *     height owner constructs a cold index during the render and the
   *     before-spacer is re-priced from its estimates in the same commit. This
   *     one rides the HEIGHT-SYNC slot (heightAnchorPendingRef) and its
   *     consumer, not shiftAnchorRef — see its capture below.
   *
   * All seven are "the height above the reader changed"; the correction is
   * identical, so they share this slot and the single consumer below. A parallel
   * path would fight this one for `scrollTop`, which is why append folds in here
   * rather than getting an anchor slot of its own.
   *
   * The capture is in the RENDER phase (getSnapshotBeforeUpdate idiom) for ALL.
   * That point is canonical rather than merely convenient: a post-commit read
   * cannot recover a pre-shift position — the row has already moved and the
   * delta reads zero — while a pre-shift capture is valid for the window shift
   * too, because the mounted nodes still carry the PREVIOUS commit's geometry
   * while the new range renders. Capturing here also removes the stale-anchor
   * hazard the callback capture had: an anchor taken when a shift was merely
   * SCHEDULED outlived a no-op window commit and was then applied to an
   * unrelated later one, yanking the viewport to a row nobody was reading.
   *
   * Arithmetic is no alternative: `getH` prices an unmeasured row from the
   * running MEAN of measured ones, so any measurement re-prices every unmeasured
   * row and the next sync re-reads them all (measured: a 1000px insert displaced
   * rows by 1500).
   *
   * Staged, because trigger 1 needs an extra commit before it can measure:
   *   'awaiting-rebase' — prepend captured; the window must be re-based first
   *                       (part 1) so the anchor row is still mounted to measure.
   *   'rebased'         — re-base committed; correct, then re-derive the window.
   *   'ready'           — window shift captured; correct only. No re-derive:
   *                       the shift already is the window's own decision.
   */
  const shiftAnchorRef = useRef<{ key: string; top: number } | null>(null)
  const shiftStageRef = useRef<'awaiting-rebase' | 'rebased' | 'ready' | null>(null)
  /** How far DOWN the anchored row moved in the list (new index minus old), set
   *  by TRIGGER 1's capture and consumed by part 1. Equal to the net count growth
   *  only for a pure front insert. */
  const prependCountRef = useRef(0)
  /**
   * TRIGGER 6's invalidation key for part 2, bumped in the render that captures a
   * swap anchor. Every other trigger rides a key part 2 already watches — a
   * prepend, splice or append moves `itemCount`, a window shift moves
   * `windowRange` — and an equal-count swap moves neither, so without this the
   * consumer would not run in the commit the anchor was taken for.
   *
   * Real state rather than a ref token, for the reason `heightCommit` is: a
   * counter this effect does not subscribe to is invisible to tooling and needs
   * an exhaustive-deps exemption to sit in the dep array at all. The bump is the
   * render-time state-update pattern the session and cache sentinels above use,
   * and terminates for the same reason they do: `prependPrevRef` has already
   * advanced by the time React re-invokes the render, so the re-render detects no
   * swap, captures nothing, and bumps nothing.
   *
   * Cost is one extra render pass, and only on a commit that actually captures:
   * the capture is gated on `!stickRef.current`, so the primary reading mode
   * (pinned to the bottom) never pays it, and a token append — the commit that
   * lands per streamed chunk — is not a swap and never reaches the bump.
   */
  const [spliceCommit, setSpliceCommit] = useState(0)
  /** One-shot latch for the spliceCommit bump. Main's original termination
   *  argument -- 'prependPrevRef has already advanced by the time React
   *  re-invokes the render' -- assumed a RENDER-phase mirror advance. The
   *  mirror now advances at COMMIT (a discarded concurrent attempt advancing
   *  it in render poisoned the baseline; phone rig: uncompensated landings),
   *  so a render-phase bump would re-detect the same swap on the re-invoked
   *  render and loop. The latch makes the bump once-per-commit; it is
   *  cleared where the mirror advances. */
  const spliceBumpLatchRef = useRef(false)
  /** Inserted count carried from part 1's rebase to part 2's consume, so a
   *  consume-time anchor miss (row unmounted between commits) can fall back
   *  to tree arithmetic instead of leaving the prepend uncompensated. Zero
   *  for the non-prepend triggers, which keeps the fallback inert there. */
  const shiftInsertedRef = useRef(0)
  /** scrollTop read at the trigger-1 capture render, pre-layout. -1 = unset. */
  const prependPreScrollTopRef = useRef(-1)
  /** Net count growth of the landing (itemCount - previous count), recorded
   *  at capture whether or not an anchor survived. Part 1's arithmetic
   *  fallback compensates by this when no row can be measured; the anchored
   *  path re-bases by the DISPLACEMENT in prependCountRef instead, which
   *  equals the net only for a pure front insert. */
  const prependNetRef = useRef(0)
  /** Set by part 1 in the commit it schedules a re-base in, cleared by part 2 in
   *  that same commit. Part 2 now also watches `itemCount` (for trigger 3), so
   *  it shares a commit with part 1 and would otherwise consume a prepend anchor
   *  before the re-base kept its row mounted — see part 2. */
  const rebaseScheduledRef = useRef(false)
  /** Keys of rows that LEFT the list in this render, handed to the height owner,
   *  which drains them later in this same render (see useHeightOwner). */
  const retiredKeysRef = useRef<string[] | null>(null)
  /** Display-key renames detected this render (stable id survived, key
   *  changed). Drained render-phase beside retiredKeysRef. */
  const renamedKeysRef = useRef<[string, string][] | null>(null)
  /** Previous render's identity. `items` is held because `itemsRef` has already
   *  advanced by the time the capture runs, while the mounted nodes still carry
   *  the PREVIOUS commit's indices. `getKey` is held WITH them: a caller's
   *  getKey may be index-addressed (ChatPage resolves a per-render deduped key
   *  LIST), so only the getKey of the same render prices these items correctly —
   *  the current render's closure would return the NEW list's key at the old
   *  index, misnaming the anchor by the inserted count. */
  const prependPrevRef = useRef<{
    session: string
    heightScopeKey: string | undefined
    count: number
    firstKey: string | null
    items: T[]
    getKey: (it: T, i: number) => string
  }>({
    session: sessionId, heightScopeKey, count: itemCount, firstKey: null, items, getKey,
  })
  const prependPrev = prependPrevRef.current
  const prependFirstKey = itemCount > 0 ? getKey(items[0], 0) : null
  // Guards the shared slot: a prepend capture in THIS render must not then be
  // overwritten by the window-shift branch below (a re-base changes the range).
  let anchorCapturedThisRender = false
  // A front-insert grows the count AND changes index 0's key. A slot switch does
  // both, hence the session guard; a plain append leaves index 0 alone.
  const _t1Armed =
    itemCount > prependPrev.count &&
    prependPrev.session === sessionId &&
    prependPrev.firstKey !== null &&
    prependFirstKey !== prependPrev.firstKey &&
    !stickRef.current
  if (_t1Armed) {
    const prependEl = scrollerRef.current

    // Anchor identity for the CROSS-COMMIT prepend hop. Display keys are the
    // wrong currency here: a turn takes its LEAD item's key, so the page
    // joining the top turn renames it -- and when that giant turn is the ONLY
    // visible row (a phone viewport routinely shows one), there is no
    // surviving key to fall forward to (field counters: wLost=2 on a real
    // phone, each a full-page lurch). getStableId -- the row's TAIL message --
    // survives the regroup by construction, so the SAME giant row anchors
    // across the landing. Previous items resolve through the getKey captured
    // WITH them when no stable id is provided -- see prependPrevRef's doc.
    const stableFn = getStableIdRef.current
    const idOfPrev = (it: T, i: number) => (stableFn ? stableFn(it, i) : prependPrev.getKey(it, i))
    const idOfNew = (it: T, i: number) => (stableFn ? stableFn(it, i) : getKey(it, i))
    // Current id -> current index. Membership says the ROW survived; the index
    // says where it went. That DISPLACEMENT -- not the net count growth, which
    // equals it only for a pure front insert -- is what the re-base and the
    // correction move by: a rebuild that also grows the TAIL (a reconnect
    // catching up) moves the reader by less than the count grew. Last-wins on
    // a duplicate id; callers keep row identity unique.
    const newIndexById = new Map<string, number>()
    for (let i = 0; i < items.length; i++) newIndexById.set(idOfNew(items[i], i), i)
    let prependAnchor = prependEl
      ? captureTopAnchorFrom(prependEl, elIndexRef.current.entries(), (idx) => {
          const it = prependPrev.items[idx]
          if (!it) return null
          const k = idOfPrev(it, idx)
          return newIndexById.has(k) ? k : null
        })
      : null
    let prependShift = 0
    // Net count and pre-layout scrollTop are recorded whether or not an anchor
    // survived: the anchor-miss fallback in part 1 compensates by arithmetic
    // and must still fire (leaving the count at 0 on a miss made part 1 stand
    // down entirely -- phone rig: the reader took the full inserted height as
    // a visible lurch). The arithmetic subtracts whatever native CSS scroll
    // anchoring already corrected by consume time (WebKit ships none, so the
    // remainder there is the full height) -- writing the full height on top
    // of a native correction DOUBLES the compensation.
    const inserted = itemCount - prependPrev.count
    prependNetRef.current = inserted
    prependPreScrollTopRef.current = prependEl ? prependEl.scrollTop : -1
    if (prependAnchor) {
      prependShift = newIndexById.get(prependAnchor.key)! - prependAnchor.index
    } else if (prependEl) {
      // Passes `idOfPrev`, not `prependPrev.getKey`: in this scope identity is the
      // STABLE id when the caller supplied one, and `newIndexById` is keyed that
      // way. Handing the helper the plain key would look up ids that map is not
      // keyed by, find no survivors, and silently fall back to the net count.
      const shiftAt = nearestSurvivorShiftFrom(
        prependPrev.items, idOfPrev, newIndexById, inserted,
      )
      // No visible row kept its identity. That is the shape of a wholesale
      // transcript rebuild (the post-turn refresh re-identifying every row it
      // streamed) landing together with the front growth, and standing down
      // here leaves the window and scrollTop where they were -- which, with
      // rows now in front, is the START of the transcript rather than the
      // rows being read. So the topmost visible row is re-identified by
      // POSITION: it moved by as much as the NEAREST row (by old index) whose
      // identity did survive, and its new identity is whatever now sits at
      // old index + that displacement. Only when no identity survives
      // anywhere does the net count stand in -- the reader then keeps their
      // distance from the END, the one thing a full re-identification of a
      // chat transcript preserves.
      prependAnchor = captureTopAnchorFrom(prependEl, elIndexRef.current.entries(), (idx) => {
        const j = idx + shiftAt(idx)
        const it = items[j]
        return it ? idOfNew(it, j) : null
      })
      if (prependAnchor) prependShift = shiftAt(prependAnchor.index)
    }
    // Part 1 re-bases by the reader's own displacement, in either direction --
    // rows coalescing ABOVE the reader while the tail grows moves them UP even
    // though the count grew -- which is what keeps the anchored row mounted for
    // part 2 to measure. A displacement of zero leaves nothing to re-base; a
    // height change above an unmoved row is trigger 7's case, not this one.
    if (prependAnchor && prependShift !== 0) {
      shiftAnchorRef.current = prependAnchor
      shiftStageRef.current = 'awaiting-rebase'
      prependCountRef.current = prependShift
      anchorCapturedThisRender = true
    } else if (prependAnchor) {
      // Anchored but unmoved: the landing did not displace the reader's rows,
      // so the arithmetic fallback must not fire either (compensating an
      // insert that is not above the reader would itself be the lurch).
      prependNetRef.current = 0
      prependPreScrollTopRef.current = -1
    }
  }
  // ---- Count-change classification, read BEFORE the mirror advances ----
  //
  // What the branches below need is which PRE-EXISTING INDICES moved, because the
  // mounted nodes in `elIndexRef` carry the PREVIOUS commit's indices: a node's
  // index still names its own row after a tail append, and names the WRONG row
  // after any splice above it.
  const sameSessionCount = prependPrev.session === sessionId && prependPrev.firstKey !== null
  // A front insert renames index 0 (trigger 1's case) and a slot switch changes
  // the session; both are excluded from everything below.
  const frontKeyHeld = prependFirstKey === prependPrev.firstKey
  // Did any PRE-EXISTING position change hands? That one question separates a
  // tail append from a mid-list insert, and detects a same-count swap.
  //
  // Exact, not sampled. The cheap proxy this replaces read only the LAST
  // pre-existing index, which a replacement anywhere ABOVE it satisfies while
  // still stranding the replaced row's measurement -- so an artifact card
  // refreshed in place, or a row replaced while another is appended, left a
  // height in the mean that no live row justified.
  //
  // Cost is a scan, but not a re-keying one: the overwhelmingly common commit is
  // a token append, which rebuilds the array while REUSING every element object
  // except the streaming row's. Reference equality settles those rows without
  // calling `getKey` at all, so the usual commit costs N pointer comparisons and
  // zero allocation. A key is only computed for a position whose object actually
  // changed, which is the only place a departure can hide.
  const sharedCount = Math.min(prependPrev.count, itemCount)
  let movedIndex = -1
  for (let i = 0; i < sharedCount; i++) {
    const prevItem = prependPrev.items[i]
    const nextItem = items[i]
    if (prevItem === nextItem) continue
    if (prevItem === undefined || nextItem === undefined) { movedIndex = i; break }
    // The PREVIOUS item is priced through the getKey captured WITH it, the NEW
    // one through this render's closure: an index-addressed getKey (ChatPage's
    // deduped key list) returns the new list's key at an old index, which would
    // report every position as moved on an ordinary append.
    if (prependPrev.getKey(prevItem, i) !== getKey(nextItem, i)) { movedIndex = i; break }
  }
  const anyIndexMoved = movedIndex >= 0
  const grewInSession = itemCount > prependPrev.count && sameSessionCount && frontKeyHeld
  /** TRIGGER 3 — the count grew and nothing pre-existing moved. */
  const tailAppended = grewInSession && !anyIndexMoved
  /** TRIGGER 4 — a row appeared above at least one row that is already mounted. */
  const midListInserted = grewInSession && anyIndexMoved
  /** TRIGGER 5 — a row LEFT the list, with index 0 held. `frontKeyHeld` is the
   *  ANCHOR's requirement, not retirement's: a renamed index 0 means the mounted
   *  nodes' indices no longer name their own rows, so there is nothing to anchor
   *  on. Retirement has its own gate below and deliberately does not share this
   *  one. */
  const rowsRemoved = itemCount < prependPrev.count && sameSessionCount && frontKeyHeld
  /** TRIGGER 6 — an equal-count SWAP: a shared index changed hands while the count
   *  stood still, which is the placeholder leaving and its replacement arriving in
   *  one React-batched commit. Same `frontKeyHeld` requirement as 4 and 5, for the
   *  same reason (the mounted nodes' indices must still name their own rows), and
   *  the indices do not even shift here — only one row's identity does. */
  const rowSwapped = itemCount === prependPrev.count && sameSessionCount && frontKeyHeld && anyIndexMoved
  // TRIGGERS 4, 5 and 6 — a mid-list splice: a row in, a row out, or one row
  // traded for another. All three capture the anchor, through the one capture
  // point and the one consumer: each is "a row came or went above the reader",
  // and the correction part 2 already performs does not care which direction it
  // moved. Placed BEFORE trigger 2 so that in a render which does both, the
  // splice's key mapping wins over the window branch's live-items mapping — the
  // whole point being that live-items mapping is what is wrong here.
  //
  // The equal-count SWAP is here rather than excluded because it now brings its
  // own invalidation key (`spliceCommit`, bumped below). Before that key existed
  // the anchor had no consumer on a commit that moves neither `windowRange` nor
  // `itemCount`, so capturing would have stranded it in the slot for an unrelated
  // later commit to spend — see #7234, and `spliceCommit`'s own doc.
  //
  // Staged 'ready' (correct only, never a re-base): a transient row moves the
  // anchor by one index, so it stays inside the mounted window and is
  // measurable. A splice wide enough to unmount it leaves `rowTopFrom` unable to
  // resolve the row and part 2 stands down — the pre-existing behaviour for an
  // unmeasurable anchor, not a new failure mode.
  //
  // The GATE is departure, not any one trigger. Retirement kept escaping through
  // whichever count arithmetic a commit happened not to match -- an equal-count
  // swap, an interior replacement, and a full-transcript clear each reached this
  // point with a row's measurement still pricing the transcript. Those are one
  // defect with three faces, so the condition is stated once, at the level the
  // harm lives on: A ROW LEFT THIS SESSION. A departure requires either a
  // shrinking count or a shared index changing hands, so the streaming commit
  // (same rows, one more at the tail) still does no work here.
  //
  // `frontKeyHeld` is deliberately NOT part of it. It is the anchor's
  // requirement, and borrowing it for retirement is what let the clear through:
  // emptying the list renames index 0 exactly as head paging does, so the proxy
  // read a wipe as a page-out and kept every measurement.
  const rowDeparturePossible =
    sameSessionCount && (itemCount < prependPrev.count || anyIndexMoved)
  if (rowDeparturePossible) {
    const survivingKeys = new Set<string>()
    for (let i = 0; i < items.length; i++) survivingKeys.add(getKey(items[i], i))
    // The anchor keeps the narrower gate: it needs index 0 held (so the mounted
    // nodes' indices still name their own rows) and a commit whose consumer will
    // actually run — a count change for triggers 4 and 5, and for trigger 6 the
    // `spliceCommit` bump below, which is what an equal-count commit has instead.
    // Retirement has neither dependency, which is why it sits outside.
    if ((midListInserted || rowsRemoved || rowSwapped) && !anchorCapturedThisRender && !stickRef.current) {
      const spliceEl = scrollerRef.current
      let spliceAnchor = spliceEl
        ? captureTopAnchorFrom(spliceEl, elIndexRef.current.entries(), (idx) => {
            // PREVIOUS items at the node's PREVIOUS index, filtered to rows that
            // survive this commit — trigger 1's resolution, for the same reason:
            // it is the only mapping that names the row the node actually shows.
            const it = prependPrev.items[idx]
            if (!it) return null
            const k = prependPrev.getKey(it, idx)
            return survivingKeys.has(k) ? k : null
          })
        : null
      if (!spliceAnchor && spliceEl) {
        // No visible row kept its key — the splice landed together with a total
        // re-identification of the on-screen rows (the wholesale-rebuild shape
        // TRIGGER 1 already covers; the splice half was deferred there and is
        // #8033). Standing down leaves scrollTop where it was and displaces the
        // reader by the spliced rows' height, so the topmost visible row is
        // re-identified by POSITION, exactly as TRIGGER 1 does: for rows below
        // the splice point the displacement is the inserted-above count, which
        // the nearest surviving old-index neighbour carries whenever any row
        // between the splice and the reader keeps its key. `movedIndex` is the
        // change boundary: survivors above it did not move, and borrowing their
        // zero displacement would anchor a splice landing exactly at the
        // viewport top on the inserted row itself.
        const newIndexByKey = new Map<string, number>()
        for (let i = 0; i < items.length; i++) newIndexByKey.set(getKey(items[i], i), i)
        const shiftAt = nearestSurvivorShiftFrom(
          prependPrev.items, prependPrev.getKey, newIndexByKey,
          itemCount - prependPrev.count, Math.max(movedIndex, 0),
        )
        // The positional anchor names a row of the NEW list, so it is priced by
        // the CURRENT render's getKey paired with the current items — the same
        // pairing contract as TRIGGER 1's fallback.
        spliceAnchor = captureTopAnchorFrom(spliceEl, elIndexRef.current.entries(), (idx) => {
          const j = idx + shiftAt(idx)
          const it = items[j]
          return it ? getKey(it, j) : null
        })
      }
      if (spliceAnchor) {
        shiftAnchorRef.current = spliceAnchor
        shiftStageRef.current = 'ready'
        anchorCapturedThisRender = true
        // Only the swap needs the bump — the other two already move `itemCount`,
        // and bumping there would buy an extra render pass for a consumer that
        // was going to run anyway. The three shapes are mutually exclusive by
        // their count arithmetic, so this cannot double-fire.
        if (rowSwapped && !spliceBumpLatchRef.current) {
          spliceBumpLatchRef.current = true
          setSpliceCommit((n) => n + 1)
        }
      }
    }
    // Which measurements this commit retires or renames -- see
    // planHeightRetirement. `onTopReached` is read from the prop, not its ref:
    // the ref is refreshed in an effect, so during the render a consumer first
    // wires paging in it still holds the previous value.
    const plan = planHeightRetirement({
      prevItems: prependPrev.items,
      prevGetKey: prependPrev.getKey,
      items,
      getKey,
      survivingKeys,
      getStableId: getStableIdRef.current,
      pagingConsumer: onTopReached !== undefined,
      countFell: itemCount < prependPrev.count,
    })
    if (plan.renamed.length > 0) renamedKeysRef.current = plan.renamed
    if (plan.retired.length > 0) retiredKeysRef.current = plan.retired
  }
  // The mirror advances at COMMIT, not in render. Under concurrent
  // rendering (the transcript arrives through useDeferredValue) React can
  // run this body and then DISCARD the attempt: a render-phase advance in a
  // discarded attempt poisons the baseline, so the attempt that commits
  // compares against its own snapshot, arms nothing, and the landing goes
  // entirely uncompensated (phone rig: kilopixel shifts with no capture /
  // rebase events anywhere near them -- desktop CPUs rarely interrupt, so
  // the tear only surfaced under 4x throttle). Committing the advance also
  // makes interleaved renders CUMULATIVE: attempts A->B and A->C within one
  // commit both compare against A, so the committed capture spans every
  // page that landed, not just the last attempt's slice.
  const prependMirrorNext = { session: sessionId, heightScopeKey, count: itemCount, firstKey: prependFirstKey, items, getKey }
  useLayoutEffect(() => {
    prependPrevRef.current = prependMirrorNext
    spliceBumpLatchRef.current = false
  })

  // TRIGGER 2 capture. Read BEFORE the mirror advances, so the comparison is
  // against the range that is still on screen. Keyed on the range's START
  // having ACTUALLY moved in committed state — not on a shift being scheduled —
  // which is what makes a no-op window commit incapable of stranding an anchor.
  // A re-base in flight owns the slot: part 1 moving the range UP (a negative
  // displacement) reads here exactly like a window shift, and capturing again
  // would replace the prepend anchor with a row of the not-yet-corrected frame.
  //
  // EITHER direction. Rows mounting above the reader are re-priced from the
  // estimate (the upward case this trigger was written for); rows UNMOUNTING
  // above the reader are replaced by a spacer priced from the tree, and that
  // price equals the DOM they replace only while the tree is current. During
  // a width transition it is not: the gate keeps the old width's heights out
  // of reach of the new width's re-wraps, so a scroll recompute that walks
  // the start down hands the reader a spacer short by every re-wrap it swept
  // up (probe, anchoring off: three table rows +320 each unmounted in one
  // recompute, the reader's row 960px higher with nothing to correct it —
  // Chromium's native anchoring absorbed the same shift, WebKit has none).
  // A downward shift over current prices measures a zero delta and writes
  // nothing, so the consumer's cost there is the read alone.
  if (
    !anchorCapturedThisRender &&
    shiftStageRef.current !== 'rebased' &&
    windowRange.start !== windowRangeRef.current.start &&
    !stickRef.current
  ) {
    const shiftEl = scrollerRef.current
    const shiftAnchor = shiftEl
      ? captureTopAnchorFrom(shiftEl, elIndexRef.current.entries(), (idx) => {
          const it = items[idx]
          return it ? (getStableIdRef.current ? getStableIdRef.current(it, idx) : getKey(it, idx)) : null
        })
      : null
    if (shiftAnchor) {
      shiftAnchorRef.current = shiftAnchor
      shiftStageRef.current = 'ready'
      anchorCapturedThisRender = true
    }
  }
  // TRIGGER 3 capture. Same slot, same stage as trigger 2: an append needs the
  // correction only, never a re-base — existing indices do not move, so the
  // anchor row is already mounted. Keyed on the window start having stayed put,
  // which is what separates this from trigger 2 (an upward shift) and keeps the
  // two from double-capturing in one render.
  //
  // This capture is what makes the correction possible at all: the DOM read here
  // is the PREVIOUS commit's geometry, so it records where the reader's row was
  // BEFORE the re-pricing lands. The offset tree is re-synced later in this same
  // render (see the `offsetIndex` memo), and after that commit the row has
  // already moved — a post-commit read would measure zero drift.
  if (!anchorCapturedThisRender && tailAppended && windowRange.start === windowRangeRef.current.start && !stickRef.current) {
    const appendEl = scrollerRef.current
    const appendAnchor = appendEl
      ? captureTopAnchorFrom(appendEl, elIndexRef.current.entries(), (idx) => {
          const it = items[idx]
          return it ? getKey(it, idx) : null
        })
      : null
    if (appendAnchor) {
      shiftAnchorRef.current = appendAnchor
      shiftStageRef.current = 'ready'
    }
  }

  // The height owner's beforeNotify (see HeightIndex.syncAndAnnounce): runs
  // after the tree mutated and before the re-render the announcement schedules.
  const captureHeightSyncAnchor = useCallback(() => {
    // Spacer repricing about to commit: rows ABOVE the viewport re-price
    // (estimates replaced by real heights), which moves everything below by
    // the delta. Chrome's native scroll anchoring absorbs that shift; iOS
    // Safari has none, so a reader sees the transcript slide under their
    // finger (measured 13-25px right after a far jump, when a whole streak
    // of first measurements lands in one sync). Capture the top visible row
    // now so the anchor-compensation layout effect below can hold it steady
    // across the commit. Skipped while stick is armed -- the bottom pin owns
    // positioning there.
    //
    // This capture can run MID-TRANSACTION: a prepend's eager first
    // measurements fire it after the DOM mutated but before the shift
    // effect writes its scrollTop correction, so the captured top reads
    // UNCOMPENSATED geometry. Left alone, consuming it after the shift
    // write measures the row "moved back" and reverses the correction —
    // net zero, reader dropped on the pagination sentinel, infinite
    // loading (measured: paired deltas [+2144,-2144], [+6680,-6680] per
    // page). The shift consumer therefore RE-BASELINES this anchor right
    // after its own write (see the window effect), so what lands here is
    // only the residual. Skipping the capture instead was tried and left a
    // hole: with continuous scrolling re-arming the stage every few frames,
    // whole 120ms re-measure batches went uncompensated — a controlled
    // fixed-velocity probe saw a 749px one-frame lurch with every anchor
    // counter silent.
    //
    // An UNCONSUMED capture is kept, not overwritten. The consumer clears the
    // slot in the commit each announcement schedules, so a capture is still
    // pending here only when both land in ONE commit: TRIGGER 7 captures the
    // reader's row against the last committed frame in the swap's render, and
    // the mounted-row reseed announces the cold owner's first prices in that
    // commit's layout phase -- against a DOM showing the cold spacer. Re-
    // capturing there would baseline on that intermediate frame and
    // pay back only the reseed's move, leaving the swap's own move uncorrected
    // (probe: row 41 -> 111). The earlier capture is the painted one, and its
    // scrollTop guard (heightAnchorStillUsable) still drops it if the reader
    // moved in between.
    if (heightAnchorPendingRef.current) return
    if (!stickRef.current && scrollerRef.current) {
      const a = captureTopAnchorFrom(scrollerRef.current, elIndexRef.current.entries(), (i) => {
        const it = itemsRef.current[i]
        return it ? getKeyRef.current(it, i) : null
      })
      if (a) heightAnchorPendingRef.current = { cands: captureAnchorCands(scrollerRef.current), at: performance.now(), scrollTop: scrollerRef.current.scrollTop }
    } else if (stickRef.current && scrollerRef.current) {
      // FOLLOWED reader: no anchor to capture (the bottom pin owns the
      // position), but the consumer's PRE-PAINT re-pin still has to run --
      // and its entry guard is this very slot. Leaving it empty starved that
      // branch, so a followed reader's repricing fell through to the
      // POST-PAINT pinAuto: one visible frame of displacement per landing
      // wave. That is the "opens, then starts jumping a moment later"
      // report — the measure farm reaches deep-idle ~5s after open and
      // reprices estimate-priced rows in batches from then on.
      //
      // A SENTINEL (no candidates) is what the slot needs: the stick branch
      // reads only live geometry (bottomTarget), never these candidates, so
      // an empty list is not a missing measurement — it says "a reprice is
      // committing, re-pin before paint".
      heightAnchorPendingRef.current = { cands: [], at: performance.now(), scrollTop: scrollerRef.current.scrollTop }
    }
  }, [scrollerRef, captureAnchorCands, stickRef, elIndexRef, itemsRef, getKeyRef])

  // TRIGGER 7 capture -- the WIDTH-SCOPE swap. Every other geometry change
  // that can move the reader announces through HeightIndex.syncAndAnnounce,
  // whose beforeNotify is the capture above. The scope swap is the one that
  // does not: the height owner constructs the COLD index for the new width
  // during this very render and the same render reads the before-spacer from
  // it, so the spacer goes from the old width's measured prefix to the new
  // scope's flat estimates in one commit while scrollTop stays put (probe:
  // same window, spacerBefore 9353 -> 3500, scrollTop unchanged, one blank
  // frame, then a row 70 places away under the reader). The rows above the
  // reader legitimately have no height at the new width -- that is the
  // bucket working -- so the discontinuity cannot be measured away, only
  // ANCHORED. Same slot and same consumer as the height sync. The
  // mounted-row reseed announces the cold owner's first prices in this
  // commit's layout phase, so the consumer runs in the re-render that
  // follows, re-reads the row against the reseeded spacer and pays the whole
  // move -- last committed frame to reseeded frame -- in one write (probe:
  // spacer +5784.5, scrollTop +5785, reader row held to the half-pixel). The
  // reseed's own beforeNotify capture stands down for this pending one (see
  // captureHeightSyncAnchor), which is what makes it exactly once. Without a
  // `canMeasure` gate there is no reseed, and the swap commits unanchored as
  // it always did -- both shipped hosts pass the gate.
  //
  // Gated to a SAME-SESSION swap over an unchanged list: the capture prices
  // mounted nodes through the CURRENT items at the nodes' previous indices,
  // which names the row a node shows only when no index changed hands. A true
  // session switch replaces every row (the reading-position entry places the
  // reader, not this), and a swap landing in the same commit as a list change
  // is that change's trigger: its consumer measures the row's actual
  // displacement, spacer included.
  if (
    heightScopeKey !== prependPrev.heightScopeKey &&
    sameSessionCount &&
    itemCount === prependPrev.count &&
    !anyIndexMoved
  ) {
    captureHeightSyncAnchor()
  }

  // A restore places the reader at an absolute offset priced against the
  // transcript that already contains any rows a pending capture was taken for,
  // so it drops that capture before it writes (see restoreAnchor).
  const dropShiftCapture = useCallback(() => {
    shiftAnchorRef.current = null
    shiftStageRef.current = null
    shiftInsertedRef.current = 0
    prependPreScrollTopRef.current = -1
  }, [])

  return {
    shiftAnchorRef,
    shiftStageRef,
    prependCountRef,
    shiftInsertedRef,
    prependPreScrollTopRef,
    prependNetRef,
    rebaseScheduledRef,
    heightAnchorPendingRef,
    spliceCommit,
    retiredKeysRef,
    renamedKeysRef,
    captureAnchorCands,
    captureHeightSyncAnchor,
    dropShiftCapture,
  }
}

export interface ShiftCompensation {
  /** The resize observer's same-fire correction for rows repriced above the
   *  fold. Returns whether it wrote. */
  compensateAboveFold: (el: HTMLDivElement, aboveFoldReprice: number) => boolean
}

/** The consuming half, all pre-paint layout effects. Called after the height
 *  owner (its version keys the height-sync consumer) and before the follow
 *  owner's placement pins, which must see these corrections. */
export function useShiftCompensation<T>(ctx: {
  itemCount: number
  windowRange: WindowRange
  heightCommit: number
  /** This render's owner, synced for this render -- a value, not a ref. */
  offsetIndex: HeightIndex
  scrollerRef: RefObject<HTMLDivElement | null>
  elIndexRef: Ref<Map<Element, number>>
  itemsRef: Ref<T[]>
  anchorIdOf: (item: T, index: number) => string
  setWindowRange: SetWindowRange
  shift: ShiftCapture
  follow: Pick<FollowState, 'stickRef' | 'writeScrollTop'>
  pinning: Pick<Pinning, 'prePaintRepin'>
  reading: Pick<ReadingPositionEntry, 'settleMeasuringRef'>
  ops: Pick<WindowOperations, 'recomputeWindow'>
}): ShiftCompensation {
  const {
    itemCount, windowRange, heightCommit, offsetIndex, scrollerRef, elIndexRef, itemsRef, anchorIdOf, setWindowRange,
  } = ctx
  const {
    shiftAnchorRef, shiftStageRef, prependCountRef, shiftInsertedRef, prependPreScrollTopRef, prependNetRef,
    rebaseScheduledRef, heightAnchorPendingRef, spliceCommit, captureAnchorCands,
  } = ctx.shift
  const { stickRef, writeScrollTop } = ctx.follow
  const { prePaintRepin } = ctx.pinning
  const { settleMeasuringRef } = ctx.reading
  const { recomputeWindow } = ctx.ops

  /** The owner whose announcements the height-sync consumer is paying -- an
   *  identity change there is the width-scope swap, not a reprice. */
  const heightOwnerSeenRef = useRef<HeightIndex>(offsetIndex)

  const compensateAboveFold = useCallback((el: HTMLDivElement, aboveFoldReprice: number): boolean => {
    // Hold a RELEASED reader against a reprice above them, in this fire. The
    // amount is the residual the measurement owner computed against the
    // reader's row's last seen position (see measureResizeEntries), so it is
    // whatever the engine's native anchoring left undone -- the whole batch
    // where there is none. A followed reader is deliberately excluded: the
    // pin below already puts them at the bottom, and adding this would move
    // them twice.
    //
    // This runs BEFORE the rail-settle deferral because it is not part of the
    // write storm that deferral exists to hold back: it is one write per
    // batch, and deferring it is precisely the delay that makes the
    // displacement visible.
    // A restore owns the position while its gate is up (see the shift-consume
    // effect): repricing rows above the fold shifts the reader to stay still,
    // which fights an absolute placement rather than preserving it.
    if (shiftCompensationAllowed({ stick: stickRef.current, settleMeasuring: settleMeasuringRef.current }) && Math.abs(aboveFoldReprice) > 0.5) {
      writeScrollTop(el, el.scrollTop + aboveFoldReprice, 'auto', 'pin', 'abovefold')
      return true
    }
    return false
  }, [writeScrollTop, stickRef, settleMeasuringRef])

  /**
   * Part 1 — TRIGGER 1 only: re-base the window by the anchored row's own
   * displacement so the rows being read stay mounted, including the anchor row
   * that part 2 has to measure. Runs pre-paint, so the shifted-but-uncorrected frame is never
   * shown. A window shift needs no equivalent: it IS a range change already.
   */
  useLayoutEffect(() => {
    const armed = shiftStageRef.current === 'awaiting-rebase'
    const net = prependNetRef.current
    prependNetRef.current = 0
    if (!armed && net <= 0) return
    const shift = prependCountRef.current
    prependCountRef.current = 0
    const el = scrollerRef.current
    // Every exit from 'awaiting-rebase' clears the slot: an anchor left in that
    // stage is one part 2 never consumes.
    if (stickRef.current || !shiftAnchorRef.current || shift === 0) {
      // Same standing-down rule as the consume effect below: a restore owns the
      // position while its gate is up, and this arithmetic compensation would
      // double-count the block the restore already priced in.
      if (el && net > 0 && shiftCompensationAllowed({ stick: stickRef.current, settleMeasuring: settleMeasuringRef.current })) {
        // ANCHOR-MISS FALLBACK: no surviving row to measure against (and the
        // positional re-identification found nothing either), so compensate
        // by arithmetic instead of standing down. The offset tree was synced
        // render-phase this commit, and a top-walk page lands only on
        // farm-measured geometry, so the inserted block's height is exact
        // there (and a fair estimate elsewhere -- either beats a full-page
        // lurch). Same-commit pre-paint: rebase the window so mounted rows
        // keep their identity, then advance scrollTop by the block just
        // inserted above the reader.
        setWindowRange((r) => ({
          start: Math.min(itemCount, r.start + net),
          end: Math.min(itemCount, r.end + net),
        }))
        let insertedPx = 0
        for (let i = 0; i < net; i++) insertedPx += offsetIndex.getHeight(i)
        // Reading scrollTop forces layout, which is also when native scroll
        // anchoring applies its own correction -- so the read already
        // includes it. Write only what is still missing.
        const preTop = prependPreScrollTopRef.current
        const nativeAdj = preTop >= 0 ? el.scrollTop - preTop : 0
        const remainder = insertedPx - Math.max(0, nativeAdj)
        if (remainder > 0.5) writeScrollTop(el, el.scrollTop + remainder, 'auto', 'pin', 'reprice1')
      }
      prependPreScrollTopRef.current = -1
      shiftAnchorRef.current = null
      shiftStageRef.current = null
      return
    }
    shiftStageRef.current = 'rebased'
    shiftInsertedRef.current = net
    rebaseScheduledRef.current = true
    // Signed: the anchored row's displacement, so the re-based range contains it
    // whichever way it moved. Clamped to the list on both ends.
    const clamp = (i: number) => Math.max(0, Math.min(itemCount, i))
    setWindowRange((r) => ({ start: clamp(r.start + shift), end: clamp(r.end + shift) }))
    // itemCount is the ONLY trigger by design: offsetIndex re-syncs in the
    // same render that changes itemCount (its memo keys on it), and the
    // scroller/write helpers are stable -- re-running on their identity
    // would re-fire a consumed prepend.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [itemCount])

  /**
   * Trigger-1 stage promotion: 'rebased' -> 'ready' on the commit whose
   * windowRange change IS part 1's re-base landing (rebaseScheduledRef
   * marks it). Keying on windowRange -- not on effect-run counting -- is
   * what makes the promotion immune to extra effect ticks: however many
   * times the consumer re-runs between part 1 and the re-based commit,
   * the stage stays 'rebased' and the anchor stays uncommitted until the
   * re-based rows are really in the DOM.
   */
  useLayoutEffect(() => {
    if (rebaseScheduledRef.current && shiftStageRef.current === 'rebased') {
      rebaseScheduledRef.current = false
      shiftStageRef.current = 'ready'
    }
    // windowRange is the invalidation key: the promotion must observe the
    // commit that applied part 1's setWindowRange.
  }, [windowRange, rebaseScheduledRef, shiftStageRef])

  /**
   * Part 2 — the single consumer for TRIGGERS 1-6: re-read the anchor row in
   * the shifted DOM and move scrollTop by however far it travelled, which holds
   * the user's place whatever mix of inserted rows and re-estimated heights
   * caused the shift.
   *
   * Gated on the stage, not on this effect having run: the deps include
   * `recomputeWindow`, whose identity changes as heights are measured, so an
   * ungated read here would consume a prepend anchor before part 1 had re-based
   * the window to keep its row mounted.
   *
   * The scrollTop write is recorded in lastWriteTopRef so the passive scroll
   * listener classifies it as a self-scroll (isSelfScroll / SELF_SCROLL_EPSILON)
   * and does not release stick or treat it as user input.
   *
   * After a TRIGGER 1 correction only (`insertedForFallback` marks it), re-derive the window: the passive
   * itemCount recompute has already run by this point — React flushes it between
   * these two layout effects — so it sized the window from the PRE-correction
   * offset and its update lands after this write. Re-deriving from the corrected
   * scrollTop is what stops that stale range being the one left committed. A
   * window-shift correction must NOT re-derive: that range is the
   * window's own decision, and recomputing it here would fight the scroll.
   */
  useLayoutEffect(() => {
    // Consume at 'ready' ONLY. A trigger-1 anchor is promoted to 'ready'
    // by the effect above, keyed on the windowRange commit that actually
    // mounted the re-based rows. The previous protocol accepted 'rebased'
    // here behind a one-shot stand-down flag; under a throttled CPU the
    // flag was consumed by an earlier same-flush run and the anchor was
    // then measured against the TRANSITIONAL DOM -- new items over the old
    // commit's node indices -- whose mixed coordinates either mis-bound at
    // the captured position (delta 0: correction swallowed, the landing
    // showed as a full-page lurch) or bound across the spacer (a
    // kilopixel over-correction, snapped back a frame later). Phone rig:
    // both signatures, per-landing.
    const stage = shiftStageRef.current
    if (stage !== 'ready') return
    shiftStageRef.current = null
    const pending = shiftAnchorRef.current
    shiftAnchorRef.current = null
    const insertedForFallback = shiftInsertedRef.current
    shiftInsertedRef.current = 0
    const prependPreTopForFallback = prependPreScrollTopRef.current
    prependPreScrollTopRef.current = -1
    const el = scrollerRef.current
    if (!el) return
    // A restore OWNS the scroll position while its gate is up, so no shift
    // compensation may add to it. These corrections keep the reader visually
    // still across a PREPEND -- they add the inserted block's height to
    // scrollTop -- but a restore does not need keeping still: it placed the
    // reader at an absolute offset computed against the transcript that
    // ALREADY CONTAINS those rows (`heightIndexRef.offsetOf`). Applying the
    // compensation on top double-counts the whole block. Measured on a phone
    // as `WRITE restore 3021->965` immediately followed by
    // `WRITE reprice2 965->20211` -- one deciseconds apart, +19,246px, landing
    // the reader at the bottom of a session they had left near the top, which
    // then read as "switching back during a stream always lands at the end".
    //
    // `stickRef` alone could not cover this: it stands down for follow-the-tail,
    // which is a DIFFERENT owner of the position. Both are cases of a
    // correction measured against one position being applied to another.
    if (!shiftCompensationAllowed({ stick: stickRef.current, settleMeasuring: settleMeasuringRef.current })) return
    // CONSUME-MISS FALLBACK: the anchor row can vanish between capture and
    // consume (unmounted by a concurrent recompute on a slow device). For a
    // prepend the compensation is still knowable by arithmetic -- the block
    // inserted above the reader -- so apply that instead of returning with
    // the reader uncompensated (phone rig: per-landing kilopixel shifts).
    const fallbackCompensate = () => {
      if (insertedForFallback <= 0) return
      let px = 0
      for (let i = 0; i < insertedForFallback; i++) px += offsetIndex.getHeight(i)
      // Subtract what native scroll anchoring already corrected since the
      // capture render (see prependPreScrollTopRef) -- a blind full-height
      // write on top of it doubles the compensation into a page-sized leap.
      const preTop = prependPreTopForFallback
      const nativeAdj = preTop >= 0 ? el.scrollTop - preTop : 0
      const remainder = px - Math.max(0, nativeAdj)
      if (remainder > 0.5) writeScrollTop(el, el.scrollTop + remainder, 'auto', 'pin', 'reprice2')
    }
    if (!pending) { fallbackCompensate(); if (insertedForFallback > 0) recomputeWindow(); return }
    const newTop = rowTopFrom(el, elIndexRef.current.entries(), (idx) => {
      const it = itemsRef.current[idx]
      return it ? anchorIdOf(it, idx) : null
    }, pending.key)
    if (newTop === null) { fallbackCompensate(); if (insertedForFallback > 0) recomputeWindow(); return }
    const delta = newTop - pending.top
    // Instant, and accounted as a 'pin' write: this is our own correction, so
    // the follow guard must recognise the resulting scroll event as self-scroll
    // rather than user input. Routed through the chokepoint so the accounting
    // cannot be forgotten here (see writeScrollTop).
    if (Math.abs(delta) > 0.5) {
      writeScrollTop(el, el.scrollTop + delta, 'auto', 'pin', 'resize')
      // Re-baseline a pending height anchor: its capture may have read the
      // UNCOMPENSATED geometry mid-transaction (see syncHeightsNow). After
      // this write the row sits where the reader sees it, so refreshing the
      // stored top makes the height consumer correct only the RESIDUAL —
      // neither reversing this write (the paired-delta loop) nor going blind
      // to the re-measure batch (the 749px uncompensated lurch).
      if (heightAnchorPendingRef.current) {
        // RE-CAPTURE, not re-price: the pending candidates may have been
        // captured mid-prepend-commit, where itemsRef had advanced while
        // elIndex still carried pre-shift indices — every priced key was off
        // by the inserted count, so mapping them forward preserves the tear.
        // Here the rebase has committed and this write just landed, so a
        // fresh capture reads a CONSISTENT pairing; the height consumer then
        // corrects only what moves after this point.
        heightAnchorPendingRef.current = { cands: captureAnchorCands(el), at: performance.now(), scrollTop: el.scrollTop }
      }
    }
    // Trigger-1 only (`insertedForFallback` marks it): the passive
    // itemCount recompute sized the window from the PRE-correction offset,
    // so re-derive from the corrected scrollTop. A window-shift ('ready'
    // from capture) correction must NOT re-derive -- that range is the
    // window's own decision, and recomputing would fight the scroll.
    if (insertedForFallback > 0) recomputeWindow()
    // `itemCount` is an invalidation key, not a value this body reads: TRIGGER 3
    // captures in a render that changes no windowRange, so without it the
    // correction would wait for an unrelated window commit and be applied to
    // geometry that had already drifted. `spliceCommit` is the same kind of key
    // for TRIGGER 6, which moves neither of the other two.
  // eslint-disable-next-line react-hooks/exhaustive-deps -- trigger set is deliberate (see comment above)
  }, [windowRange, itemCount, spliceCommit, scrollerRef, writeScrollTop, recomputeWindow])

  // Same correction for a HEIGHT-SYNC commit (spacer repricing), keyed on the
  // owner's announced version. See heightAnchorPendingRef for why this cannot
  // share the window effect's slot.
  //
  // `heightCommit` is the invalidation key: the effect must run in the commit the
  // announcement scheduled, and the version identifies it. Unlike the counter this
  // replaced, it cannot go stale or be forgotten -- the owner bumps it in the same
  // call that mutates the tree, so there is no bump site to miss. It is also a
  // real subscribed value rather than a token invisible to tooling, which is why
  // no exhaustive-deps exemption is needed here any more.
  //
  // TRIGGER 7 (the width-scope swap) captures in the swap's own render but is
  // NOT paid in the swap's commit. That commit renders the cold owner's
  // flat-estimate spacer, and the mounted-row reseed (useMeasurementScopeReseed)
  // announces the cold owner's first real prices in that commit's layout phase,
  // so the re-render follows in the same task. Paying the swap against the cold
  // spacer would move scrollTop into a coordinate system nothing else shares:
  // the passive window recompute that follows the swap (and the scroll event
  // the write itself raises) map that scrollTop through the now-reseeded tree
  // and unmount the rows under the reader. Consuming in the announced commit
  // instead measures the row's move from the last PAINTED geometry to the
  // reseeded one in a single write. The owner's identity is a key so the swap
  // commit is SEEN (and stood down from) even when the two owners' versions
  // are equal -- see the guard at the top of the effect.
  useLayoutEffect(() => {
    // An owner IDENTITY change is not an announcement. The swap commit reaches
    // here because the two owners' versions happen to differ (a warm owner has
    // announced at least once; a cold one never has), or -- when they are equal
    // -- because `offsetIndex` is a key of this effect: either way the pending
    // capture stays in the slot for the reseed's announced commit, which is
    // where the geometry it must be paid against is committed.
    if (heightOwnerSeenRef.current !== offsetIndex) {
      heightOwnerSeenRef.current = offsetIndex
      // The swap commit's cold document can be SHORTER than the reader's
      // position: every unmounted row is priced at the flat estimate, so the
      // engine clamps scrollTop to the new ceiling with no application write
      // anywhere (both engines, at a matched depth: captured 35365, cold
      // scrollHeight 22983 against a 900 viewport, scrollTop 22083). The
      // reseed grows the document back but scrollTop stays at the ceiling, so
      // the freshness guard below would read that drop as the reader's and
      // drop the payment: one blank frame, then a row far above under the
      // reader. Re-base the capture to the clamped value -- and ONLY when the
      // live scrollTop is exactly the ceiling a capture above it was dragged
      // to. The candidates keep their painted geometry, so the reseed pays the
      // whole move; a drop that is not the ceiling (native anchoring, 14662
      // for the same capture) is left alone and the guard drops it as before;
      // and anything that moves the viewport after the clamp still differs
      // from the re-based value, so the guard still wins. No input can land
      // between this render and its layout phase, which is what makes the
      // ceiling coincidence the clamp and nothing else.
      const pending = heightAnchorPendingRef.current
      const el = scrollerRef.current
      if (pending && el) {
        const ceiling = el.scrollHeight - el.clientHeight
        if (pending.scrollTop > ceiling && heightAnchorStillUsable(ceiling, el.scrollTop)) {
          heightAnchorPendingRef.current = { ...pending, scrollTop: el.scrollTop }
        }
      }
      return
    }
    const pending = heightAnchorPendingRef.current
    heightAnchorPendingRef.current = null
    if (!pending) return
    const el = scrollerRef.current
    if (!el || typeof el.getBoundingClientRect !== 'function') return
    if (stickRef.current) {
      prePaintRepin(el)
      return
    }
    // STALE ANCHOR = GARBAGE, but staleness is about whether the VIEWPORT
    // moved, not about the clock. The hazard is a viewport-relative capture
    // consumed after the reader moved: correcting by that delta corrects their
    // own scrolling (the cold-cache walk teleport, 2706px on the phone rig,
    // three runs out of three). A wall-clock age was the first approximation
    // and it fails on the wrong side at the worst moment: a turn ending is the
    // busiest the main thread gets (transcript rebuild, regroup, a whole batch
    // of repricing), so this effect runs late, a STILL reader's anchor is
    // dropped by age, and they pay the entire reprice as one displacement --
    // reported as the transcript moving down a long way the moment a turn
    // ended. Deliberately BELOW the stick branch: the bottom re-pin reads only
    // LIVE geometry (bottomTarget), so staleness cannot mis-correct it.
    //
    // The exact discriminator is scrollTop: a reprice ABOVE the viewport moves
    // where rows sit, it does not move scrollTop. An unchanged scrollTop
    // therefore means the delta is entirely the reprice's and correcting it is
    // right however late it lands; any change means something else moved the
    // viewport -- the reader's finger, iOS momentum (which keeps moving with no
    // further hard input, so an input-timestamp gate would miss it), or
    // Chromium's native anchoring (which has already absorbed the shift, so
    // the correction is a no-op worth skipping anyway).
    if (!heightAnchorStillUsable(pending.scrollTop, el.scrollTop)) return
    // Released reader: the correction IS the candidates, so an empty list is
    // nothing to consume (the followed path above uses a candidate-free
    // sentinel and has already returned by here).
    if (pending.cands.length === 0) return
    let newTop: number | null = null
    let capturedTop = 0
    for (const cand of pending.cands) {
      const t = rowTopFrom(el, elIndexRef.current.entries(), (idx) => {
        const it = itemsRef.current[idx]
        return it ? anchorIdOf(it, idx) : null
      }, cand.key)
      if (t !== null) { newTop = t; capturedTop = cand.top; break }
    }
    if (newTop === null) return
    const delta = newTop - capturedTop
    if (Math.abs(delta) > 0.5) {
      writeScrollTop(el, el.scrollTop + delta, 'auto', 'pin', 'growth')
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps -- heightCommit and the owner's identity are the triggers; geometry is read live
  }, [heightCommit, offsetIndex, scrollerRef, writeScrollTop])

  return { compensateAboveFold }
}
