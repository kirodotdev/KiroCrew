import { useEffect, useRef, type MutableRefObject } from 'react'
import { useFileWatch, type FileWatchFrame } from './useFileWatch'

/**
 * Where a side-panel tab stands against its backing file since it last became
 * the visible tab.
 *
 *  * `due` -- a catch-up read has to run at the next clean moment: the tab is
 *    hidden, it has left edit mode, or the surface marked it so (an edit to a
 *    clean buffer, a read the user's typing overtook).
 *  * `reading` -- one is in flight, and a re-render starts no second one.
 *  * `done` -- nothing to do until the tab is hidden or edited again.
 *
 * Only the surface's own read procedure writes `done`, through
 * {@link settleCatchUp}, once its answer is on screen: starting a read never
 * counts as being current.
 */
export type CatchUpLatch = 'due' | 'reading' | 'done'

/**
 * Settle the catch-up read that answered. Not unconditionally `done`: a read
 * that lands after the tab was hidden again finds the latch back at `due`, and
 * the next return must read afresh.
 */
export function settleCatchUp(latch: MutableRefObject<CatchUpLatch>): void {
  if (latch.current === 'reading') latch.current = 'done'
}

interface VisibleTabCatchUpOptions {
  /** The backing file; null watches nothing. */
  path: string | null
  /** Whether this tab is the one on screen. */
  active: boolean
  /** Whether the tab may hold a stream while it is on screen. */
  watchArmable: boolean
  /** Whether a catch-up read may start now. */
  catchUpArmable: boolean
  /** Edit mode. Leaving it owes a read: the stretch spent editing held no stream. */
  editing: boolean
  /** The surface's latch, shared with its read procedure. */
  latch: MutableRefObject<CatchUpLatch>
  /** Start the catch-up read. It settles the latch on its outcome. */
  catchUp: () => unknown
  /** One `/api/file-watch` frame; the surface decides whether it says anything new. */
  onFrame: (content: string, frame: FileWatchFrame) => void
}

/**
 * One `/api/file-watch` stream for the visible tab only, and a catch-up read
 * when a tab comes back.
 *
 * `SidePanel` keeps every document tab mounted and only toggles which one shows.
 * Each stream is one EventSource -- one HTTP/1.1 connection held for as long as
 * it is open -- and Chromium allows six per host, so a stream per mounted tab
 * lets a handful of idle tabs starve every other dashboard request (uploads,
 * chat sends, polls). A hidden tab therefore holds no stream. What it misses
 * while hidden it picks up here: becoming the visible tab starts the surface's
 * catch-up read, so a change made meanwhile lands the moment the user can see
 * the tab rather than never. The read does not wait for the re-armed stream to
 * say anything: a saturated connection budget is exactly what delays that
 * stream, and a file emptied meanwhile produces no connect-time frame at all
 * (`api_file_watch` compares against an initial empty string).
 *
 * The surface owns what a read and a frame mean -- its buffer, its cache, its
 * edit guards -- and supplies them as `catchUp` and `onFrame`; this hook owns
 * when they run.
 */
export function useVisibleTabCatchUp({
  path,
  active,
  watchArmable,
  catchUpArmable,
  editing,
  latch,
  catchUp,
  onFrame,
}: VisibleTabCatchUpOptions): void {
  useFileWatch(watchArmable && active ? path : null, onFrame)

  const wasEditingRef = useRef(editing)
  useEffect(() => {
    if (wasEditingRef.current && !editing) latch.current = 'due'
    wasEditingRef.current = editing
    if (!active) { latch.current = 'due'; return }
    if (latch.current !== 'due' || !catchUpArmable) return
    latch.current = 'reading'
    void catchUp()
  }, [active, editing, catchUpArmable, catchUp, latch])
}
