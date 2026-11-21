import { useSyncExternalStore } from 'react'

/**
 * The one fold query: two viewport segments side by side, which a book-style
 * foldable reports while it is partly folded. The CSS that follows a fold
 * (FOLD_TWO_PANE_QUERY in the `md` variant and the top bar ladder) repeats this
 * text verbatim, and viewportSegmentsParity.test.ts fails on any other segments
 * query under src/, so CSS and script always agree on "folded".
 */
export const FOLD_X_QUERY = '(horizontal-viewport-segments: 2)'

/** The narrowest viewport where the two-pane form fits beside a fold: the
 *  compact rail, the 180px sessions list and a usable transcript. */
export const FOLD_TWO_PANE_MIN = 640

/**
 * A reported fold at FOLD_TWO_PANE_MIN and wider. The layout takes its two-pane
 * form here ahead of the 768px breakpoint and the touch-first phone rule
 * (lib/touchLayoutForm.ts). The `md` variant in tailwind-theme.css repeats it
 * verbatim, so the `md:` styles follow the same form; the parity test pins both.
 */
export const FOLD_TWO_PANE_QUERY = `(min-width: ${FOLD_TWO_PANE_MIN}px) and ${FOLD_X_QUERY}`

/** One segment's horizontal extent, in CSS pixels from the viewport's left edge. */
export interface ViewportSegment {
  readonly x: number
  readonly width: number
}

// Both APIs are behind feature flags and absent from the DOM typings.
type SegmentsViewport = { segments?: ArrayLike<{ x: number; width: number }> | null }
type PostureNavigator = Navigator & { devicePosture?: EventTarget }

let cachedKey = ''
let cached: readonly ViewportSegment[] | null = null

function read(): readonly ViewportSegment[] | null {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return null
  if (!window.matchMedia(FOLD_X_QUERY).matches) return null
  const segs = (window as Window & { viewport?: SegmentsViewport }).viewport?.segments
  if (!segs || segs.length < 2) return null
  // Width and x only: the simulator reports a segment height unrelated to the
  // viewport's, and no layout here places anything by it.
  const next = Array.from(segs, s => ({ x: s.x, width: s.width }))
  const key = next.map(s => `${s.x}+${s.width}`).join(',')
  // One array per distinct reading, so useSyncExternalStore sees a stable snapshot.
  if (key !== cachedKey) { cachedKey = key; cached = next }
  return cached
}

function subscribe(cb: () => void) {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return () => {}
  const mql = window.matchMedia(FOLD_X_QUERY)
  // Posture says that the device folded but not where, so it only wakes a re-read.
  const posture = (navigator as PostureNavigator).devicePosture
  mql.addEventListener?.('change', cb)
  window.addEventListener('resize', cb)
  posture?.addEventListener?.('change', cb)
  return () => {
    mql.removeEventListener?.('change', cb)
    window.removeEventListener('resize', cb)
    posture?.removeEventListener?.('change', cb)
  }
}

const getServerSnapshot = () => null

/**
 * The viewport's two side-by-side segments while the device reports a fold, or
 * `null`: on every browser without the Viewport Segments API, on a flat or
 * closed foldable (one segment), and whenever {@link FOLD_X_QUERY} does not match.
 */
export function useViewportSegments(): readonly ViewportSegment[] | null {
  return useSyncExternalStore(subscribe, read, getServerSnapshot)
}
