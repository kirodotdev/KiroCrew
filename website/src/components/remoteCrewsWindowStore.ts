/**
 * The Remote Crews window's open request (#41685), kept apart from the window
 * so the switcher, the pane relay and the Settings panel can reach it without
 * pulling the window into the app's first-load chunk. One window per
 * dashboard: the state is a module store, like the crew pins.
 */
/** Closed, open on the crew list, or opened for Add a Crew alone (`add`: the list stays hidden behind it). */
export type WindowState = null | { add: boolean }

let current: WindowState = null

/** The window's current request, for `useSyncExternalStore`. */
export function getRemoteCrewsWindowState(): WindowState { return current }
const listeners = new Set<() => void>()
function emit() { listeners.forEach(cb => cb()) }

// Settings owns its existing panel. Hold Add until that panel mounts after
// navigation, then consume it once rather than remounting or opening a window.
let pendingPanelAdd = false
const panelAddListeners = new Set<() => void>()
function deliverPanelAdd() {
  const listener = panelAddListeners.values().next().value
  if (!pendingPanelAdd || !listener) return
  pendingPanelAdd = false
  listener()
}
export function requestRemoteCrewPanelAdd() {
  pendingPanelAdd = true
  deliverPanelAdd()
}
export function subscribeRemoteCrewPanelAdd(listener: () => void) {
  panelAddListeners.add(listener)
  deliverPanelAdd()
  return () => { panelAddListeners.delete(listener) }
}

/** Open the window; `add` opens it with Add a Crew's first step on top. */
export function openRemoteCrewsWindow(add = false) {
  current = { add }
  emit()
}

/** Turn an Add-only request into the crew list. */
export function showRemoteCrewsList() {
  current = { add: false }
  emit()
}

export function closeRemoteCrewsWindow() {
  current = null
  emit()
}

export function subscribeRemoteCrewsWindow(cb: () => void) {
  listeners.add(cb)
  return () => { listeners.delete(cb) }
}
