import { useSyncExternalStore } from 'react'
import { safeSetItem } from '../utils/safeStorage'

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

/** Reactive read of the current dock position + a setter that broadcasts. */
export function useSidePanelDock(): [SidePanelDock, (d: SidePanelDock) => void] {
  const dock = useSyncExternalStore(subscribe, () => current, () => current)
  return [dock, setSidePanelDock]
}
