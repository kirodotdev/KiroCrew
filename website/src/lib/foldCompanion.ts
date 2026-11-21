/**
 * The chat page's panes in book pose: a fold reported between two side-by-side
 * viewport segments, with three panes (sessions list, transcript, side panel)
 * and two segments to put them in. In priority order:
 *
 *  1. The transcript always keeps a segment.
 *  2. An open side panel takes the other segment, at that segment's reported
 *     width, and the sessions list steps aside (`sidebarOpen` reads false; the
 *     stored pin is untouched, so it comes back when the panel closes, and
 *     flattening restores the layout the device had before the fold). This
 *     is the "companion" layout: the transcript in the leading segment, the
 *     panel in the trailing one.
 *  3. With the panel closed, the sessions list holds the leading segment and the
 *     transcript the trailing one (the chat page split).
 *
 * Asking for the sessions list while the companion holds (the sessions toggle,
 * the rail's chat item, the shortcut) closes the panel instead of pinning a list
 * that has no segment to go to: see `yieldFoldCompanion`.
 *
 * Widths and x offsets only: the segment heights a browser reports are not
 * read (see `useViewportSegments`).
 */
import { useEffect, useLayoutEffect, useRef, useState, useSyncExternalStore } from 'react'
import type { ViewportSegment } from '../hooks/useViewportSegments'
import { runPaneAllocationChange } from './layoutFormTransition'

/** The segment opposite the leading one: the right-hand segment in a
 *  left-to-right document, the left-hand one in a right-to-left one. Segments
 *  are listed left to right. */
export function trailingSegment<T>(segments: readonly T[], leading: 'left' | 'right'): T {
  return leading === 'right' ? segments[0] : segments[segments.length - 1]
}

/** The safe-area insets the shell's `p-safe` grid keeps clear at the left and
 *  right edges of the viewport (`safeAreaLeft`, `safeAreaRight`). */
export interface SafeInsets { left: number; right: number }
const NO_INSETS: SafeInsets = { left: 0, right: 0 }

/** The part of `segment` inside the shell's content box: the viewport the
 *  segments span, less the safe-area insets at either edge. A pane sized to
 *  the whole segment would start or end an inset past the box, so the shell
 *  would push it across the fold. The fold gap between segments is left
 *  out either way, since neither segment includes it. */
export function segmentContentWidth(
  segment: ViewportSegment, segments: readonly ViewportSegment[], insets: SafeInsets = NO_INSETS,
): number {
  const last = segments[segments.length - 1]
  const boxStart = segments[0].x + insets.left
  const boxEnd = last.x + last.width - insets.right
  return Math.max(0, Math.min(segment.x + segment.width, boxEnd) - Math.max(segment.x, boxStart))
}

/** The side panel's width in the companion layout: the trailing segment's
 *  part inside the shell's content box, so the panel's inline-start edge sits
 *  on the fold and its far edge on the box's. */
export function foldPanelWidth(
  { segments, leading, insets }: { segments: readonly ViewportSegment[]; leading: 'left' | 'right'; insets?: SafeInsets },
): number {
  return segmentContentWidth(trailingSegment(segments, leading), segments, insets)
}

/** The width of the fold itself between the two segments: 0 where they meet,
 *  as on the iPhone Duo, and the masked hinge on a device that reports one. */
export function foldGapWidth(segments: readonly ViewportSegment[]): number {
  const [a, b] = segments
  return Math.max(0, b.x - (a.x + a.width))
}

// Whether the full chat page is showing the companion layout. The shell reads it
// to hold the rail compact, and the sessions-list requests read it at call time.
// One writer: the full chat page (an embedded or popped-out chat has no list).
let shown = false
const listeners = new Set<() => void>()

export function setFoldCompanionShown(next: boolean): void {
  if (next === shown) return
  shown = next
  listeners.forEach(l => l())
}

/** Synchronous read, for event handlers. */
export function foldCompanionShown(): boolean {
  return shown
}

function subscribe(cb: () => void) {
  listeners.add(cb)
  return () => { listeners.delete(cb) }
}

/** Reactive read, for the shell. */
export function useFoldCompanionShown(): boolean {
  return useSyncExternalStore(subscribe, foldCompanionShown, () => false)
}

/**
 * A request to show the sessions list. While the companion holds, the list has
 * no segment of its own, so the panel closes and the list takes the leading
 * segment back (the chat page split). Returns whether it yielded; the caller
 * then makes sure the list is pinned rather than toggling it, because the list
 * the person asked for was hidden, not shown.
 */
export function yieldFoldCompanion(closePanel: () => void): boolean {
  if (!shown) return false
  closePanel()
  return true
}

/**
 * What a reveal request does while the companion layout holds: pin the list and
 * close the panel. It is an explicit request to show the list, like the other
 * reveal paths, so it also clears the preview-expand auto-hide (`autoHidden`),
 * whose end would otherwise restore the pin it saved and hide the list again.
 */
export function foldYieldToList(
  { autoHidden, setPinned, closePanel }: { autoHidden: { current: boolean | null }; setPinned: (pinned: boolean) => void; closePanel: () => void },
): () => void {
  return () => {
    autoHidden.current = null
    setPinned(true)
    closePanel()
  }
}

/**
 * A request to show a session or folder in the list (the title menu, a folder
 * chip, the command bar) made while the companion layout holds: the panel gives
 * up the trailing segment and the list is pinned to take the leading one back,
 * where it consumes the request (`yieldToList` does both). A new chat's
 * folder-open request (`openFoldersOnly`) is not a request to show the list.
 * A request already in the store when the layout appeared is older than it (no
 * list was mounted to consume it) and is left alone: replaying it would close
 * every panel opened beside the fold. Requests are told apart by `nonce`.
 */
export function useFoldCompanionYieldsToReveal(
  { companion, revealRequest, yieldToList }: { companion: boolean; revealRequest: { nonce: number; openFoldersOnly?: boolean } | null; yieldToList: () => void },
): void {
  // The newest request this layout has seen; null while it is not on screen.
  const seen = useRef<number | null>(null)
  useEffect(() => {
    if (!companion) { seen.current = null; return }
    const nonce = revealRequest?.nonce ?? -1
    if (seen.current === null) { seen.current = nonce; return }
    if (nonce === seen.current) return
    seen.current = nonce
    if (!revealRequest || revealRequest.openFoldersOnly) return
    yieldFoldCompanion(yieldToList)
    // eslint-disable-next-line react-hooks/exhaustive-deps -- reacts to a new request or the layout; `yieldToList` is rebuilt each render
  }, [companion, revealRequest])
}

/**
 * Whether the companion layout applies: a fold is reported, the page is in its
 * desktop form, the side panel is on screen, and the page has a sessions list
 * to step aside (the full chat page, not an embedded or popped-out chat).
 */
export function foldCompanionApplies(
  { segments, desktop, listHost, panelShown }: { segments: readonly ViewportSegment[] | null; desktop: boolean; listHost: boolean; panelShown: boolean },
): boolean {
  return segments !== null && desktop && listHost && panelShown
}

/**
 * The companion layout on screen: `wanted` (foldCompanionApplies), one render
 * behind it when the panel opens or closes beside a fold that holds still. That
 * render keeps the old layout, so the morph (runPaneAllocationChange) can
 * snapshot it before the panes move. A fold, an unfold or a form switch is a
 * viewport resize with its own morph (useIsMobile), and lands at once.
 * `publish` marks the one writer of `foldCompanionShown`.
 */
export function useFoldCompanionLayout(
  { wanted, segments, isMobile, publish }: { wanted: boolean; segments: readonly ViewportSegment[] | null; isMobile: boolean; publish: boolean },
): boolean {
  const [companion, setCompanion] = useState(wanted)
  const seen = useRef({ segments, isMobile })
  // A render the viewport moved in (`seen` holds the last committed reading)
  // takes the wanted layout at once, so no render draws the old layout with the
  // new viewport: that render would hide an open panel for a frame and drop the
  // state its tabs keep while shown, an expanded preview among them.
  const viewportMoved = seen.current.segments !== segments || seen.current.isMobile !== isMobile
  const shown = viewportMoved ? wanted : companion
  useLayoutEffect(() => {
    seen.current = { segments, isMobile }
    if (companion === wanted) return
    if (viewportMoved) { setCompanion(wanted); return }
    runPaneAllocationChange(() => setCompanion(wanted), wanted)
  }, [companion, wanted, segments, isMobile, viewportMoved])
  // A layout effect, so the shell's rail settles in the commit the morph snapshots.
  useLayoutEffect(() => { if (publish) setFoldCompanionShown(shown) }, [publish, shown])
  useLayoutEffect(() => () => { if (publish) setFoldCompanionShown(false) }, [publish])
  return shown
}
