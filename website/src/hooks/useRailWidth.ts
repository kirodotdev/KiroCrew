import { useSyncExternalStore } from 'react'

/**
 * Width (px) of the app shell's left nav rail track.
 *
 * The rail is App-local state (`navCollapsed`, persisted at `mc-nav`) but its
 * width is a LAYOUT FACT that consumers outside App need: ChatPage sizes the
 * activity panel's beside-vs-fill decision against the space actually left for
 * the chat, and the rail is the first thing subtracted from it.
 *
 * Published as the resolved TRACK value (0 / 74 / 236) rather than measured
 * from the DOM on purpose. The rail's collapse is a 150ms grid-template
 * transition, so `getBoundingClientRect()` reports intermediate widths mid
 * animation — enough to flip a width gate twice per toggle. The track value
 * steps once.
 *
 * Module-level (same shape as usePanelTabs) so the value survives consumer
 * remounts and needs no context provider.
 */
const RAIL_W_EXPANDED = 236
const RAIL_W_COLLAPSED = 74

/**
 * Duration of the shell's `grid-template-columns` transition (App.tsx), plus a
 * couple of frames of slack so the window closes AFTER the final resize of the
 * animation rather than in the middle of it.
 *
 * MUST stay in step with that CSS duration. If they drift apart the window
 * either closes early (the tail of the animation resumes thrashing) or stays
 * open too long (one extra frame of held-back height sync) — neither is a
 * correctness bug, but both blunt the point of the window.
 */
const RAIL_TRANSITION_MS = 150
const RAIL_SETTLE_SLACK_MS = 40
export const RAIL_SETTLE_MS = RAIL_TRANSITION_MS + RAIL_SETTLE_SLACK_MS

/** Rail width for the shell's current state. Mobile has no rail track. */
export function railWidthFor({ isMobile, collapsed }: { isMobile: boolean; collapsed: boolean }): number {
  if (isMobile) return 0
  return collapsed ? RAIL_W_COLLAPSED : RAIL_W_EXPANDED
}

let railWidth = RAIL_W_EXPANDED
const listeners = new Set<() => void>()

/**
 * Timestamp until which the rail's collapse animation is considered in flight.
 *
 * The collapse animates a LAYOUT property (`grid-template-columns`), so the
 * content column's width changes on every frame of it and every mounted
 * transcript row rewraps. Consumers that measure the DOM in a ResizeObserver
 * therefore see ~9 frames of transitional geometry whose measurements are all
 * superseded at the final width. Measured in isolation, that inflates the
 * virtualizer's ResizeObserver work and forced layout reads by 13-18x per
 * toggle, and none of the extra measurements change the final cached heights.
 *
 * Published here — next to the width itself — because this module already owns
 * "the rail's collapse is a 150ms grid-template transition" as a fact its
 * consumers need (see the note on publishing the stepped track value).
 */
let railSettlingUntil = 0

/** True while the rail's collapse/expand animation is still in flight. */
export function isRailSettling(): boolean {
  return performance.now() < railSettlingUntil
}

/** Called by App when the rail track changes. No-op when the value is unchanged. */
export function setRailWidth(w: number) {
  if (w === railWidth) return
  railWidth = w
  // Armed HERE rather than at a separate call site: this is already the single
  // point App notifies on a track change, so the window cannot be forgotten by
  // a future edit that changes how the rail collapses.
  railSettlingUntil = performance.now() + RAIL_SETTLE_MS
  listeners.forEach(l => l())
}

function subscribe(cb: () => void) {
  listeners.add(cb)
  return () => { listeners.delete(cb) }
}

const getSnapshot = () => railWidth

export function useRailWidth() {
  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot)
}

/** Test seam: restore the module default between cases. */
export function __resetRailWidth() {
  railWidth = RAIL_W_EXPANDED
  railSettlingUntil = 0
  listeners.clear()
}
