/**
 * Keeps a pinned folder header where it is when its folder collapses.
 *
 * A list-view folder header is `position: sticky` inside its folder block, so
 * while the block's top has scrolled above the lane the header is painted at
 * its pin offset, not at the block's top. Collapsing shrinks the block to the
 * header alone, which takes away the room the header was sticking in: with an
 * unchanged `scrollTop` the header lands at the block's top, above the lane,
 * and every row below jumps up by the distance scrolled into the folder.
 *
 * The fix is to move the BLOCK, not the header: before the body starts to
 * close, scroll the lane so the block's top sits exactly where the header is
 * painted. The header's natural position then equals its pinned one, so it
 * does not move at all, and the body closes underneath it with the next folder
 * or session rising to meet it. Measuring the painted offset (rather than
 * deriving it from the sticky `top` of each depth) keeps this correct for a
 * nested header pinned below its ancestors and for a header the block's end is
 * already pushing off the lane.
 *
 * WHEN it runs matters as much as how far. The scroll lands in the same commit
 * that marks the folder collapsed, before that frame paints: `FolderBody` hides
 * its rows in that commit. Scrolling earlier, in the click handler, would paint
 * at least one frame of the folder's FIRST rows under the header (the
 * optimistic collapse reaches React a few ticks after the click), so the rows
 * the person was looking at would swap for others before folding away.
 *
 * WHERE the header was painted is read at the click, not in that commit. With
 * motion on, the body's grid track is still at its open height in the
 * collapsed commit, so the header is still pinned there; under
 * `prefers-reduced-motion` the transition is zeroed, the body is already
 * closed, and the header has fallen back to the block's top. The block's own
 * top does not move as its body closes, so the click's header position less
 * the block's top in the collapsed commit is the same distance either way.
 *
 * HOW FAST the body closes follows from the hold. Once the lane has moved, an
 * animated close slides the folder's rows up past the still header and leaves
 * the lane under it empty until the next folder arrives. So a collapse that
 * starts from a pinned header takes the reduced-motion path for that one
 * commit: the body closes with no transition (`instantClose` on `FolderBody`),
 * and the session rows' layout projection is off, because Framer would
 * otherwise animate the next folder's rows up from where they sat below the
 * open body, hundreds of px away. The next folder and its rows are under the
 * header in the same frame. A header at its natural place keeps the animated
 * close: nothing scrolls, and the folder visibly folds shut below it.
 *
 * WHAT RISES must be painted in that same frame. `.session-row` is
 * `content-visibility: auto` (index.css), so a row that was below the lane
 * has never been rendered. The browser only notices it has come on screen in
 * the rendering update after the layout that moved it, and paints it one or
 * two frames later; the folder header above it has no such rule and paints at
 * once. So the person sees the next folder's header over an empty lane for
 * those frames, with motion and without. Before the collapsed commit paints,
 * the rows that can rise into the lane get `content-visibility: visible`
 * inline, which renders them in that frame; the override is taken off once
 * the close has finished and `auto` resumes, now remembering their real size.
 * Only rows AFTER the collapsing block in document order, and only as many as
 * can reach the lane: a never-rendered row above the lane would otherwise
 * swap its placeholder height for its real one and move everything under it.
 */
import { useCallback, useEffect, useLayoutEffect, useRef, useState, type RefObject } from 'react'
import type { ChatFolder } from '../../types'
import { FOLDER_BODY_COLLAPSE_MS } from '../../components/FolderBody'
import { holdScrollFloor } from './collapseFloor'

/** How long a click's arm waits for its collapse to render. The optimistic
 *  update lands within a few ticks; this only bounds a dropped one. */
export const HOLD_ARM_TTL_MS = 2000

/** Sub-pixel slack: a header at its natural position measures within this. */
const PINNED_EPSILON_PX = 0.5

/** The viewport top of a folder block's own header, or null without one. */
export function paintedHeaderTop(block: HTMLElement | null): number | null {
  const header = block?.querySelector<HTMLElement>(':scope > [data-folder-row]')
  return header ? header.getBoundingClientRect().top : null
}

/** How far a block's header is painted below the block's own top, in px. */
function pinnedOffset(block: HTMLElement, headerTop: number): number {
  return headerTop - block.getBoundingClientRect().top
}

/**
 * Scroll `lane` so a collapsing folder block's header stays at `headerTop`,
 * where it was painted before the collapse (see the module note). A header
 * that was at its natural place (the block's top was on screen) leaves the
 * lane untouched.
 *
 * @param lane      the scroll container the sticky header pins to
 * @param block     the folder block (`[data-folder-drop]`) whose first row is the header
 * @param headerTop the header's viewport top before the collapse; defaults to where it is now
 * @returns the distance scrolled, in px (0 when nothing moved)
 */
export function holdPinnedHeaderThroughCollapse(
  lane: HTMLElement | null,
  block: HTMLElement | null,
  headerTop: number | null = paintedHeaderTop(block),
): number {
  if (!lane || !block || headerTop === null) return 0
  const pinnedBy = pinnedOffset(block, headerTop)
  if (pinnedBy <= PINNED_EPSILON_PX) return 0
  const before = lane.scrollTop
  // Near the end of the list the closing body would otherwise leave too
  // little content to stay scrolled this far (see collapseFloor.ts).
  holdScrollFloor(lane, Math.max(0, before - pinnedBy))
  lane.scrollTop = before - pinnedBy
  return before - lane.scrollTop
}

/**
 * Render, in the current frame, the session rows that can rise into `lane` as
 * `block` closes (see the module note on what rises). Returns the undo, which
 * lets the rows go back to `content-visibility: auto`.
 *
 * Document order is the lane's vertical order, so the walk stops at the first
 * row that cannot reach the lane's bottom edge even after rising by the body's
 * current height (already 0 when the body closed with no transition). Rows
 * inside a CLOSED folder are passed over without a look: `FolderBody` marks a
 * closed body `inert`, its rows take no room in the lane and so never rise,
 * and measuring one forces layout of a subtree the browser is skipping, with a
 * top that keeps growing under the closed header and would end the walk
 * before the open folder below it.
 */
export function renderRowsRisingInto(lane: HTMLElement | null, block: HTMLElement | null): () => void {
  if (!lane || !block) return () => {}
  const laneBottom = lane.getBoundingClientRect().bottom
  const header = block.querySelector<HTMLElement>(':scope > [data-folder-row]')
  const rise = Math.max(0, block.getBoundingClientRect().height - (header?.getBoundingClientRect().height ?? 0))
  const forced: HTMLElement[] = []
  for (const row of lane.querySelectorAll<HTMLElement>('.session-row')) {
    if (!(block.compareDocumentPosition(row) & Node.DOCUMENT_POSITION_FOLLOWING) || block.contains(row)) continue
    if (row.closest('[inert]')) continue
    if (row.getBoundingClientRect().top - rise >= laneBottom) break
    row.style.setProperty('content-visibility', 'visible')
    forced.push(row)
  }
  return () => { for (const row of forced) row.style.removeProperty('content-visibility') }
}

/**
 * The list view's collapse wiring. `armHold` is called from the click with the
 * folder's block; the hold itself runs in a layout effect on the first commit
 * that shows that folder collapsed (see the module note on timing). Expanding,
 * the folder leaving the tree, or the arm outliving `HOLD_ARM_TTL_MS` disarms
 * it.
 *
 * `instantCloseId` names the folder whose collapse started from a pinned
 * header (for its `FolderBody`'s `instantClose` and the rows' layout
 * projection). It is decided at the click, from the same reading the hold
 * uses, and committed before the collapse so the collapsed commit already
 * carries it. Two frames after that commit it is cleared, so later reorders
 * animate again: the first frame is the one the collapse paints in, and only
 * the second callback is sure to run after it. Clearing it then cannot animate
 * anything, because the closed track is already at `0fr`. It is also cleared
 * by the next arm, by `disarm`, by a dropped arm (on a timer at
 * `HOLD_ARM_TTL_MS`, so it never waits on a later `folders` change), and by
 * that folder being seen open again however it was expanded.
 */
export function useHoldPinnedHeaderOnCollapse(laneRef: RefObject<HTMLElement | null>, folders: ChatFolder[]) {
  const pending = useRef<{ id: string; block: HTMLElement; headerTop: number | null; at: number } | null>(null)
  const [instantCloseId, setInstantCloseId] = useState<string | null>(null)
  const releaseFrame = useRef<number | null>(null)
  const expiry = useRef<ReturnType<typeof setTimeout> | null>(null)
  // The undo for the rows rendered ahead of the rising, and the timer/frame
  // that will run it.
  const risingRows = useRef<{ undo: () => void; timer: ReturnType<typeof setTimeout> | null; frame: number | null } | null>(null)
  const releaseRisingRows = useCallback(() => {
    const r = risingRows.current
    if (!r) return
    risingRows.current = null
    if (r.timer !== null) clearTimeout(r.timer)
    if (r.frame !== null) cancelAnimationFrame(r.frame)
    r.undo()
  }, [])
  const cancelRelease = useCallback(() => {
    if (releaseFrame.current !== null) cancelAnimationFrame(releaseFrame.current)
    releaseFrame.current = null
    if (expiry.current !== null) clearTimeout(expiry.current)
    expiry.current = null
    releaseRisingRows()
  }, [releaseRisingRows])
  useEffect(() => cancelRelease, [cancelRelease])
  const armHold = useCallback((id: string, block: HTMLElement | null) => {
    const headerTop = block ? paintedHeaderTop(block) : null
    const arm = block ? { id, block, headerTop, at: performance.now() } : null
    pending.current = arm
    const pinned = block !== null && headerTop !== null && pinnedOffset(block, headerTop) > PINNED_EPSILON_PX
    cancelRelease()
    setInstantCloseId(pinned ? id : null)
    // A pinned arm whose collapse never renders would otherwise keep row
    // projection off until some later `folders` change. Expire it on its own
    // clock, exactly as the layout effect's TTL check would.
    if (pinned && arm) {
      expiry.current = setTimeout(() => {
        expiry.current = null
        if (pending.current !== arm) return
        pending.current = null
        setInstantCloseId(null)
      }, HOLD_ARM_TTL_MS)
    }
  }, [cancelRelease])
  const disarm = useCallback(() => {
    pending.current = null
    cancelRelease()
    setInstantCloseId(null)
  }, [cancelRelease])
  useLayoutEffect(() => {
    const p = pending.current
    if (!p) {
      // Seen open again with no collapse pending (expanded by any control):
      // its next collapse decides afresh.
      if (instantCloseId !== null && !folders.find(f => f.id === instantCloseId)?.collapsed) setInstantCloseId(null)
      return
    }
    // An arm whose collapse never rendered (the update was dropped) must not
    // fire on a later, unrelated collapse of the same folder.
    if (performance.now() - p.at > HOLD_ARM_TTL_MS) { pending.current = null; setInstantCloseId(null); return }
    const folder = folders.find(f => f.id === p.id)
    if (!folder) { pending.current = null; setInstantCloseId(null); return }
    if (!folder.collapsed) return
    pending.current = null
    const block = p.block.isConnected ? p.block : null
    holdPinnedHeaderThroughCollapse(laneRef.current, block, p.headerTop)
    // The arm has rendered: its TTL, an earlier collapse's instant-close
    // release and that collapse's rising rows are all finished with.
    cancelRelease()
    const r = { undo: renderRowsRisingInto(laneRef.current, block), timer: null as ReturnType<typeof setTimeout> | null, frame: null as number | null }
    risingRows.current = r
    r.timer = setTimeout(() => {
      r.timer = null
      r.frame = requestAnimationFrame(() => {
        r.frame = requestAnimationFrame(() => {
          r.frame = null
          if (risingRows.current === r) releaseRisingRows()
        })
      })
    }, FOLDER_BODY_COLLAPSE_MS)
    if (instantCloseId === p.id) {
      releaseFrame.current = requestAnimationFrame(() => {
        releaseFrame.current = requestAnimationFrame(() => {
          releaseFrame.current = null
          setInstantCloseId(null)
        })
      })
    }
  }, [folders, laneRef, instantCloseId, cancelRelease, releaseRisingRows])
  return { armHold, disarm, instantCloseId }
}
