import { useSyncExternalStore } from 'react'
import { safeSetItem } from '../utils/safeStorage'
import { useViewportSegments } from './useViewportSegments'

/**
 * Where the tabbed side panel docks: 'right' (default — a full-height column
 * on the window's right edge) or 'bottom' (a full-width row pinned under the
 * chat). Unlike usePersistedBool, this uses a module-level store so every
 * consumer live-syncs: the App shell owns the grid template, ChatPage owns the
 * open/close animation wrapper, and SidePanel owns its own chrome + resize —
 * all three must flip together the instant the user toggles the dock.
 */
export type SidePanelDock = 'right' | 'bottom'

const KEY = 'mc-side-panel-dock'

let current: SidePanelDock = (() => {
  try {
    return localStorage.getItem(KEY) === 'bottom' ? 'bottom' : 'right'
  } catch {
    return 'right'
  }
})()

const listeners = new Set<() => void>()

export function setSidePanelDock(dock: SidePanelDock) {
  if (dock === current) return
  current = dock
  safeSetItem(KEY, dock)
  listeners.forEach(l => l())
}

function subscribe(cb: () => void) {
  listeners.add(cb)
  return () => { listeners.delete(cb) }
}

/**
 * Reactive read of the dock the panel is drawn at, a setter that broadcasts,
 * and whether a fold holds the dock. While the device reports a fold the panel
 * docks right whatever is stored: a bottom row would span both segments, and
 * the panel takes the segment opposite the transcript (lib/foldCompanion.ts).
 * The stored choice is read, never written, so it comes back when the device
 * is flattened; callers hide the dock toggle while `locked` holds.
 */
export function useSidePanelDock(): [SidePanelDock, (d: SidePanelDock) => void, boolean] {
  const stored = useSyncExternalStore(subscribe, () => current, () => current)
  const locked = useViewportSegments() !== null
  return [locked ? 'right' : stored, setSidePanelDock, locked]
}
