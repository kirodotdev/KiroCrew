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

/** Rail width for the shell's current state. Mobile has no rail track. */
export function railWidthFor({ isMobile, collapsed }: { isMobile: boolean; collapsed: boolean }): number {
  if (isMobile) return 0
  return collapsed ? RAIL_W_COLLAPSED : RAIL_W_EXPANDED
}

let railWidth = RAIL_W_EXPANDED
const listeners = new Set<() => void>()

/** Called by App when the rail track changes. No-op when the value is unchanged. */
export function setRailWidth(w: number) {
  if (w === railWidth) return
  railWidth = w
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
  listeners.clear()
}
